# Vertex AI Gemini caller for Phase 3 clip and frame analysis.
# Stateless primitive — creates a fresh client per call, no shared state.
#
# Two exception families reach this module and they share no ancestry. The
# client is google-genai, which raises google.genai.errors.APIError subclasses
# (ClientError for 4xx, ServerError for 5xx) rooted at plain Exception. The
# google.api_core.exceptions types are kept because other Google libraries in
# the stack still raise them — google-cloud-storage genuinely does.
#
# This distinction is load-bearing. The retry below originally caught only
# api_core.ResourceExhausted, which google-genai never raises, so the 429
# backoff never fired once. Anything classifying a Gemini failure must handle
# genai_errors.APIError and key on the HTTP status code, not the class:
# ClientError spans 429 and 400 alike.

import json
import os
import random
import re
import sys
import time

import google.api_core.exceptions as api_exc
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

# One model per call mode. Both default to the same model today, and that is a
# cost and throughput decision rather than a finding that one model reads clips
# better: on Pro, clip-mode calls measured ~30x a frame read's prompt tokens on
# the pipeline's highest-volume call type, and exhausted the 429 backoff twice
# on a single video.
#
# The quality cost of that default is larger than first recorded. This comment
# read "roughly 1.5% of hands — but bounded and detectable"; measured across two
# videos the Flash truncation rate is ~15%, and it is not uniform. Runout streets
# carry no decisions, so losing one costs cards but no action data. A *contested*
# truncation yields a hand that looks complete and ends a street early, which
# corrupts an aggregate rather than thinning it, and the status_message marker
# names only the first unfound street — so it bounds neither how much was lost
# nor whether anything was.
#
# Consequence: Phase 5 is run with TT_CLIP_MODEL=gemini-2.5-pro, which recovers
# these. The default here stays Flash because this constant also serves Phase 3
# detection and Phase 4 step A, where the evidence does not reach and the token
# cost is highest. See ARCHITECTURE.md "The truncation rate, and why hand shape
# decides what it costs" and "Model selection is per call mode."
#
# The two constants survive precisely so that trade is reversible: setting
# TT_CLIP_MODEL alone moves clip calls to Pro without touching frame reads. That
# is now a standing operational setting, not a hypothetical rollback. Do not
# collapse them.
#
# Read once at import, because a model changing mid-run would leave the corpus
# with no record of which row came from which. The names are vendor-neutral on
# purpose.
#
# Public, not underscore-private: the provenance block on every stage row records
# which model served each call mode, and it reads these. Re-deriving them from
# os.environ at the write site would be a second mechanism for one setting — the
# thing TT_GEMINI_MODEL's retirement exists to prevent — and would silently
# disagree if the environment were ever mutated after import. Same reasoning as
# _log_usage taking `model` as a parameter: guessing mislabels every row.
CLIP_MODEL = os.environ.get("TT_CLIP_MODEL", "gemini-3.8-flash")
FRAME_MODEL = os.environ.get("TT_FRAME_MODEL", "gemini-3.8-flash")

# Media resolution, named here so call sites never carry a bare string and so
# the value sent is the value recorded. PartMediaResolutionLevel *warns* rather
# than raises on an unknown value, so a typo in a literal would degrade a read
# with no error anywhere.
#
# These are also what the provenance block stores: media resolution is a request
# parameter, and a change to one alters extraction behaviour corpus-wide without
# touching a model id or a prompt hash. Before they were recorded, rows read at
# HIGH and at ULTRA_HIGH carried byte-identical provenance and nothing in the
# data could tell them apart. See ARCHITECTURE, "Provenance."

# What call_gemini_for_clip sends: nothing. The config carries no
# media_resolution, which the API treats as unspecified. Recorded under the
# API's own name rather than as null, so every call mode reads the same way.
CLIP_MEDIA_RESOLUTION = "MEDIA_RESOLUTION_UNSPECIFIED"

# call_gemini_for_frame's request-level default, applying to any image part that
# does not override it per part.
FRAME_MEDIA_RESOLUTION = "MEDIA_RESOLUTION_HIGH"

# The per-part override the two card reads pass. See ARCHITECTURE, "Suit
# misreads are a resolution problem."
FRAME_RESOLUTION_ULTRA_HIGH = "MEDIA_RESOLUTION_ULTRA_HIGH"

REFERENCE_MEDIA_RESOLUTION = "MEDIA_RESOLUTION_LOW"

_RETRY_MAX_ATTEMPTS = 5
_RETRY_BASE_DELAY_SECONDS = 5.0
_RETRY_MAX_DELAY_SECONDS = 60.0
_RETRY_MULTIPLIER = 2.0


class GeminiTransientError(Exception):
    """Retryable: HTTP 429/5xx, timeouts, connection errors, input-token-limit 400."""


class GeminiPermanentError(Exception):
    """Non-retryable: auth, bad request, MAX_TOKENS, SAFETY, malformed JSON, empty response."""


_TRANSIENT_EXC = (
    api_exc.ResourceExhausted,
    api_exc.ServiceUnavailable,
    api_exc.DeadlineExceeded,
    api_exc.InternalServerError,
    api_exc.RetryError,
)

_PERMANENT_EXC = (
    api_exc.Unauthenticated,
    api_exc.PermissionDenied,
    api_exc.FailedPrecondition,
    api_exc.NotFound,
    api_exc.InvalidArgument,
)


def _genai_status_code(exc: Exception) -> int | None:
    """HTTP status from a google.genai error, or None if it cannot be read.

    The attribute name has moved across SDK versions — 2.7.0 exposes `code`
    only — so both spellings are tried. Deliberately no message parsing: a
    message containing "429" for an unrelated reason would otherwise retry a
    permanent failure. The isinstance check matters for the same reason; a
    string "429" is not a status this code should act on.
    """
    for attr in ("code", "status_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


def _is_rate_limited(exc: Exception) -> bool:
    """True only for a genuine HTTP 429, from either exception family."""
    if isinstance(exc, api_exc.ResourceExhausted):
        return True
    return _genai_status_code(exc) == 429


# Vertex rejects an over-large request before inference with a plain 400 naming
# both counts. The status alone cannot identify it — a genuinely malformed
# request is also a 400 — so the message is matched as well, and only in
# addition to the status, never instead of it (see _genai_status_code on why a
# bare substring match is not acceptable here).
_INPUT_TOKEN_LIMIT_RE = re.compile(
    r"input token count is (\d+) but model only supports up to (\d+)"
)

# Fixed prefix, so the attempts tables can be grouped by it across releases.
#
# Deliberately NOT a "P5-<n>: " gate id. hand_action_processing's
# _gate_failure_outcome records a step-D gate message that repeats identically
# as failed_permanent, on the reasoning that an identical repeat is an upstream
# defect no retry can reach. This message repeats identically by construction —
# the same window produces the same count — so wearing a gate id would promote
# it to permanent and undo the classification below.
#
# Not clip-specific either: the classification lives in this shared module, so
# a frame read or the payout panel read reaches it by the same path.
INPUT_TOKEN_LIMIT_CODE = "input_token_limit"


def _is_input_token_limit(exc: Exception) -> bool:
    """True only for the 400 Vertex returns when the input is too large.

    False for a 400 that merely mentions tokens: "token budget 429 exceeded" is
    a permanent failure and must stay one.
    """
    return (
        _genai_status_code(exc) == 400
        and _INPUT_TOKEN_LIMIT_RE.search(str(exc)) is not None
    )


def _input_token_limit_message(exc: Exception) -> str:
    """The fixed, countable message for an input-token-limit 400.

    Carries both reported counts, because the limit named is not stable: it is
    65,536, which is not gemini-2.5-pro's documented input limit (1,048,576) but
    exactly its *output* limit, and a measured 71,823-token request was served
    in the same run that rejected a 65,577-token one.
    """
    match = _INPUT_TOKEN_LIMIT_RE.search(str(exc))
    if match is None:  # pragma: no cover - guarded by _is_input_token_limit
        return INPUT_TOKEN_LIMIT_CODE
    return (
        f"{INPUT_TOKEN_LIMIT_CODE}: input {match.group(1)} "
        f"exceeds model limit {match.group(2)}"
    )


def _retry_exhausted_message(exc: Exception) -> str | None:
    """The message to raise if in-call retries run out, or None to re-raise now.

    Two causes are retried inside the call.

    A 429 is rate relief. An input-token-limit 400 is retried because that
    limit is enforced inconsistently — see _input_token_limit_message — and
    because Vertex does not bill a rejected request, so a second ask is the
    cheapest recovery in the system: it recovers the hand inside the attempt
    rather than leaving it for the next operator run.

    The backoff is shared rather than given a second schedule. A token-limit
    rejection needs no rate relief, but the suspected mechanism is per-backend
    variation on the `global` endpoint, where a delay plausibly helps, and a
    second delay policy is machinery this buys nothing.

    The message must name the cause. Recording an exhausted token-limit failure
    under the 429 wording would file it as a rate-limit incident, which is the
    mis-bucketing the fixed code above exists to prevent.
    """
    if _is_rate_limited(exc):
        return "rate limited by Vertex AI (429); retries exhausted"
    if _is_input_token_limit(exc):
        return f"{_input_token_limit_message(exc)}; retries exhausted"
    return None


def _classify_genai_error(exc: genai_errors.APIError) -> Exception:
    """Map a google.genai APIError onto this module's transient/permanent split.

    Keyed on the status code rather than the exception class. ClientError
    covers every 4xx including 429, so classifying by class would make 429's
    retryability depend on _call_with_retry having filtered it out first —
    exactly the kind of implicit coupling that let the original bug hide.

    An unreadable status classifies transient, matching the orchestrators'
    "anything not recognised is transient" convention: an unclassifiable
    failure should stay retryable rather than be discarded.

    The input-token-limit 400 is the one 4xx that is transient, and it is tested
    before the status rule rather than carved out of it — the limit it reports
    is enforced inconsistently, so the request may well be served on a retry.
    """
    if _is_input_token_limit(exc):
        return GeminiTransientError(_input_token_limit_message(exc))
    status = _genai_status_code(exc)
    if status is not None and 400 <= status < 500 and status != 429:
        return GeminiPermanentError(str(exc))
    return GeminiTransientError(str(exc))


def _call_with_retry(fn):
    """Call fn() with truncated exponential backoff + full jitter.

    Catches both exception families and retries a genuine 429 or an
    input-token-limit 400 — see _retry_exhausted_message for why those two and
    why they share one backoff. Anything else is re-raised untouched for the
    caller to classify.
    """
    for attempt in range(_RETRY_MAX_ATTEMPTS):
        try:
            return fn()
        except (api_exc.ResourceExhausted, genai_errors.APIError) as exc:
            exhausted_message = _retry_exhausted_message(exc)
            if exhausted_message is None:
                raise
            if attempt == _RETRY_MAX_ATTEMPTS - 1:
                raise GeminiTransientError(exhausted_message)
            cap_delay = min(
                _RETRY_BASE_DELAY_SECONDS * (_RETRY_MULTIPLIER ** (attempt + 1)),
                _RETRY_MAX_DELAY_SECONDS,
            )
            time.sleep(random.uniform(0, cap_delay))


def _log_usage(response, label: str | None, model: str) -> None:
    """Emit one greppable stderr line of token counts for a completed call.

    Phase 5 costs up to 7 calls per hand, so per-call token counts are what
    turn a run into a dollar figure. Grep with `gemini_usage`.

    Called before _parse_and_validate, so a response that fails validation
    (MAX_TOKENS, SAFETY, malformed JSON) still reports what it consumed —
    those calls are billed too.

    `model` is passed in rather than read from a module constant: with one
    constant per call mode there is no single right answer here, and guessing
    would mislabel every row's provenance.
    """
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return
    counts = {
        "prompt_tokens": getattr(usage, "prompt_token_count", None),
        "candidates_tokens": getattr(usage, "candidates_token_count", None),
        "total_tokens": getattr(usage, "total_token_count", None),
    }
    fields = [f"gemini_usage model={model}"]
    if label is not None:
        fields.append(f"label={label}")
    fields += [f"{k}={v}" for k, v in counts.items() if v is not None]
    print(" ".join(fields), file=sys.stderr)


def _parse_and_validate(response) -> dict:
    candidate = response.candidates[0]
    if candidate.finish_reason in (types.FinishReason.MAX_TOKENS, types.FinishReason.SAFETY):
        raise GeminiPermanentError(f"Gemini finish_reason={candidate.finish_reason}")

    text = response.text
    if not text:
        raise GeminiPermanentError("empty response from Gemini")

    text = text.strip()
    text = re.sub(r"^```(?:json)?\n", "", text)
    text = re.sub(r"\n```$", "", text)
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise GeminiPermanentError(f"malformed JSON from Gemini: {repr(text[:200])}")


def call_gemini_for_clip(
    prompt: str,
    video_gcs_uri: str,
    start_offset_seconds: int,
    end_offset_seconds: int,
    project_id: str,
    location: str = "global",
    *,
    user_text: str,
    reference_images: list[tuple[bytes, str, str]] | None = None,
    label: str | None = None,
) -> dict:
    client = genai.Client(vertexai=True, project=project_id, location=location)

    video_part = types.Part(
        file_data=types.FileData(file_uri=video_gcs_uri, mime_type="video/*"),
        video_metadata=types.VideoMetadata(
            start_offset=f"{start_offset_seconds}s",
            end_offset=f"{end_offset_seconds}s",
            fps=1.0,
        ),
    )
    # Video first, then the reference images as (bytes, mime_type, label), then
    # the user turn.
    #
    # Each image is preceded by a text part naming it. The scan prompt's STREET
    # VISUAL REFERENCE section describes the images by name, so three anonymous
    # blobs would leave the model inferring which is which from arrival order.
    # The label string must stay exactly "Reference image — {label}:", em dash
    # included — it is matched against the prompt's own wording, and this is the
    # configuration the PoC was validated against. Do not reword it.
    #
    # Omitting reference_images reproduces the original two-part request
    # exactly, which is why this one stays optional while user_text is required:
    # the default here is correct for every caller, not a silently-wrong
    # inherited value.
    parts = [video_part]
    for image_bytes, mime_type, image_label in (reference_images or []):
        parts.append(types.Part(text=f"Reference image — {image_label}:"))
        parts.append(types.Part(inline_data=types.Blob(data=image_bytes, mime_type=mime_type)))
    parts.append(types.Part(text=user_text))
    request_contents = types.Content(role="user", parts=parts)

    try:
        response = _call_with_retry(
            lambda: client.models.generate_content(
                model=CLIP_MODEL,
                config=types.GenerateContentConfig(system_instruction=prompt),
                contents=request_contents,
            )
        )
    except GeminiTransientError:
        raise
    except _TRANSIENT_EXC as exc:
        raise GeminiTransientError(str(exc)) from exc
    except _PERMANENT_EXC as exc:
        raise GeminiPermanentError(str(exc)) from exc
    except genai_errors.APIError as exc:
        raise _classify_genai_error(exc) from exc

    _log_usage(response, label, CLIP_MODEL)
    return _parse_and_validate(response)


def call_gemini_for_frame(
    prompt: str,
    frame_bytes: bytes,
    project_id: str,
    location: str = "global",
    mime_type: str = "image/jpeg",
    *,
    user_text: str,
    reference_images: list[tuple[bytes, str, str]] | None = None,
    frame_media_resolution: str | None = None,
    label: str | None = None,
) -> dict:
    client = genai.Client(vertexai=True, project=project_id, location=location)

    # Per-part resolution, not the request-level config below. The two are
    # different enums: config.media_resolution stops at HIGH, and only the
    # per-part PartMediaResolutionLevel offers ULTRA_HIGH. Leaving this None
    # reproduces the original request exactly, so the default is correct for
    # every caller rather than a silently-inherited value — the same reasoning
    # that keeps reference_images optional on call_gemini_for_clip.
    frame_part = types.Part(
        inline_data=types.Blob(data=frame_bytes, mime_type=mime_type),
        media_resolution=(
            types.PartMediaResolution(level=frame_media_resolution)
            if frame_media_resolution
            else None
        ),
    )

    # Frame first, then the reference images as (bytes, mime_type, label), then
    # the user turn — the same order and the same labelling as the clip caller,
    # because the prompts describe the images by name and anonymous blobs would
    # leave the model inferring which is which from arrival order. The label
    # string must stay exactly "Reference image — {label}:", em dash included;
    # it is matched against the prompt's own wording. Do not reword it.
    #
    # References are pinned LOW: they illustrate a shape, and paying the frame's
    # resolution for a thumbnail would cost tokens on every read forever.
    #
    # NO PRODUCTION CALLER PASSES reference_images TODAY. Suit references were
    # measured and rejected — they made misreads worse, not better (version E,
    # 6 of 160 against a baseline 4) — and the parameter is kept so
    # scripts/repro_card_read.py can still reproduce that result. Delete it only
    # together with that harness's D and E arms. See ARCHITECTURE, "Suit
    # misreads are a resolution problem."
    parts = [frame_part]
    for image_bytes, image_mime_type, image_label in (reference_images or []):
        parts.append(types.Part(text=f"Reference image — {image_label}:"))
        parts.append(
            types.Part(
                inline_data=types.Blob(data=image_bytes, mime_type=image_mime_type),
                media_resolution=types.PartMediaResolution(level=REFERENCE_MEDIA_RESOLUTION),
            )
        )
    parts.append(types.Part(text=user_text))
    request_contents = types.Content(role="user", parts=parts)

    try:
        response = _call_with_retry(
            lambda: client.models.generate_content(
                model=FRAME_MODEL,
                config=types.GenerateContentConfig(
                    system_instruction=prompt,
                    media_resolution=FRAME_MEDIA_RESOLUTION,
                ),
                contents=request_contents,
            )
        )
    except GeminiTransientError:
        raise
    except _TRANSIENT_EXC as exc:
        raise GeminiTransientError(str(exc)) from exc
    except _PERMANENT_EXC as exc:
        raise GeminiPermanentError(str(exc)) from exc
    except genai_errors.APIError as exc:
        raise _classify_genai_error(exc) from exc

    _log_usage(response, label, FRAME_MODEL)
    return _parse_and_validate(response)
