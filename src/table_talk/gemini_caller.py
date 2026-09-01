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
import random
import re
import sys
import time

import google.api_core.exceptions as api_exc
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

_MODEL = "gemini-2.5-pro"

_RETRY_MAX_ATTEMPTS = 5
_RETRY_BASE_DELAY_SECONDS = 5.0
_RETRY_MAX_DELAY_SECONDS = 60.0
_RETRY_MULTIPLIER = 2.0


class GeminiTransientError(Exception):
    """Retryable: HTTP 429/5xx, timeouts, connection errors."""


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


def _classify_genai_error(exc: genai_errors.APIError) -> Exception:
    """Map a google.genai APIError onto this module's transient/permanent split.

    Keyed on the status code rather than the exception class. ClientError
    covers every 4xx including 429, so classifying by class would make 429's
    retryability depend on _call_with_retry having filtered it out first —
    exactly the kind of implicit coupling that let the original bug hide.

    An unreadable status classifies transient, matching the orchestrators'
    "anything not recognised is transient" convention: an unclassifiable
    failure should stay retryable rather than be discarded.
    """
    status = _genai_status_code(exc)
    if status is not None and 400 <= status < 500 and status != 429:
        return GeminiPermanentError(str(exc))
    return GeminiTransientError(str(exc))


def _call_with_retry(fn):
    """Call fn() with truncated exponential backoff + full jitter on HTTP 429.

    Catches both exception families and retries only a genuine 429. Anything
    else is re-raised untouched for the caller to classify.
    """
    for attempt in range(_RETRY_MAX_ATTEMPTS):
        try:
            return fn()
        except (api_exc.ResourceExhausted, genai_errors.APIError) as exc:
            if not _is_rate_limited(exc):
                raise
            if attempt == _RETRY_MAX_ATTEMPTS - 1:
                raise GeminiTransientError(
                    "rate limited by Vertex AI (429); retries exhausted"
                )
            cap_delay = min(
                _RETRY_BASE_DELAY_SECONDS * (_RETRY_MULTIPLIER ** (attempt + 1)),
                _RETRY_MAX_DELAY_SECONDS,
            )
            time.sleep(random.uniform(0, cap_delay))


def _log_usage(response, label: str | None) -> None:
    """Emit one greppable stderr line of token counts for a completed call.

    Phase 5 costs up to 7 calls per hand, so per-call token counts are what
    turn a run into a dollar figure. Grep with `gemini_usage`.

    Called before _parse_and_validate, so a response that fails validation
    (MAX_TOKENS, SAFETY, malformed JSON) still reports what it consumed —
    those calls are billed too.
    """
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return
    counts = {
        "prompt_tokens": getattr(usage, "prompt_token_count", None),
        "candidates_tokens": getattr(usage, "candidates_token_count", None),
        "total_tokens": getattr(usage, "total_token_count", None),
    }
    fields = [f"gemini_usage model={_MODEL}"]
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
                model=_MODEL,
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

    _log_usage(response, label)
    return _parse_and_validate(response)


def call_gemini_for_frame(
    prompt: str,
    frame_bytes: bytes,
    project_id: str,
    location: str = "global",
    mime_type: str = "image/jpeg",
    *,
    user_text: str,
    label: str | None = None,
) -> dict:
    client = genai.Client(vertexai=True, project=project_id, location=location)

    part1 = types.Part(inline_data=types.Blob(data=frame_bytes, mime_type=mime_type))
    part2 = types.Part(text=user_text)
    request_contents = types.Content(role="user", parts=[part1, part2])

    try:
        response = _call_with_retry(
            lambda: client.models.generate_content(
                model=_MODEL,
                config=types.GenerateContentConfig(
                    system_instruction=prompt,
                    media_resolution=types.MediaResolution.MEDIA_RESOLUTION_HIGH,
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

    _log_usage(response, label)
    return _parse_and_validate(response)
