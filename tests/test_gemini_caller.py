import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import google.api_core.exceptions as api_exc
import pytest
from google.genai import errors as genai_errors
from google.genai import types

from table_talk.gemini_caller import (
    GeminiPermanentError,
    GeminiTransientError,
    _RETRY_MAX_ATTEMPTS,
    _classify_genai_error,
    _genai_status_code,
    _is_rate_limited,
    call_gemini_for_clip,
    call_gemini_for_frame,
)

PROJECT = "test-project"
PROMPT = "You are a poker analyst."
VIDEO_URI = "gs://bucket/video.mp4"
FRAME_BYTES = b"\xff\xd8\xff" + b"\x00" * 20  # fake bytes — mocked, not parsed
USER_TEXT = "Do the thing."


def _usage(prompt=1200, candidates=340, total=1540):
    usage = MagicMock()
    usage.prompt_token_count = prompt
    usage.candidates_token_count = candidates
    usage.total_token_count = total
    return usage


def _make_response(text: str, finish_reason=types.FinishReason.STOP):
    candidate = MagicMock()
    candidate.finish_reason = finish_reason
    response = MagicMock()
    response.text = text
    response.candidates = [candidate]
    # A bare MagicMock would auto-create usage_metadata and print mock reprs on
    # every call, so give every response realistic counts by default.
    response.usage_metadata = _usage()
    return response


def _patched_client(response):
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = response
    return mock_client


# --- request structure tests ---


def test_clip_request_structure():
    mock_client_inst = _patched_client(_make_response('{"ok": true}'))

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst) as mock_cls:
        call_gemini_for_clip(
            prompt=PROMPT,
            video_gcs_uri=VIDEO_URI,
            start_offset_seconds=10,
            end_offset_seconds=50,
            project_id=PROJECT,
            location="us-central1",
            user_text=USER_TEXT,
        )

    mock_cls.assert_called_once_with(vertexai=True, project=PROJECT, location="us-central1")

    kw = mock_client_inst.models.generate_content.call_args.kwargs
    assert kw["model"] == "gemini-2.5-pro"

    contents = kw["contents"]
    assert contents.role == "user"
    assert len(contents.parts) == 2

    p0 = contents.parts[0]
    assert p0.file_data.file_uri == VIDEO_URI
    assert p0.file_data.mime_type == "video/*"
    assert p0.video_metadata.start_offset == "10s"
    assert p0.video_metadata.end_offset == "50s"
    assert p0.video_metadata.fps == 1.0

    assert contents.parts[1].text == USER_TEXT


def test_frame_request_structure():
    custom_bytes = b"\x01\x02\x03"
    mock_client_inst = _patched_client(_make_response('{"ok": true}'))

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst) as mock_cls:
        call_gemini_for_frame(
            prompt=PROMPT,
            frame_bytes=custom_bytes,
            project_id=PROJECT,
            location="us-east1",
            mime_type="image/png",
            user_text=USER_TEXT,
        )

    mock_cls.assert_called_once_with(vertexai=True, project=PROJECT, location="us-east1")

    kw = mock_client_inst.models.generate_content.call_args.kwargs
    assert kw["model"] == "gemini-2.5-pro"

    contents = kw["contents"]
    assert contents.parts[0].inline_data.data == custom_bytes
    assert contents.parts[0].inline_data.mime_type == "image/png"
    assert contents.parts[1].text == USER_TEXT

    assert kw["config"].media_resolution == types.MediaResolution.MEDIA_RESOLUTION_HIGH


def test_clip_user_text_override():
    mock_client_inst = _patched_client(_make_response('{"ok": true}'))

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        call_gemini_for_clip(
            prompt=PROMPT,
            video_gcs_uri=VIDEO_URI,
            start_offset_seconds=10,
            end_offset_seconds=50,
            project_id=PROJECT,
            user_text="Identify the first voluntary chip commitment and second action in this video window.",
        )

    kw = mock_client_inst.models.generate_content.call_args.kwargs
    contents = kw["contents"]
    assert contents.parts[1].text == (
        "Identify the first voluntary chip commitment and second action in this video window."
    )


def test_frame_user_text_override():
    mock_client_inst = _patched_client(_make_response('{"ok": true}'))

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        call_gemini_for_frame(
            prompt=PROMPT,
            frame_bytes=FRAME_BYTES,
            project_id=PROJECT,
            user_text="Extract hole cards for all eligible players from this frame.",
        )

    kw = mock_client_inst.models.generate_content.call_args.kwargs
    contents = kw["contents"]
    assert contents.parts[1].text == "Extract hole cards for all eligible players from this frame."


def test_clip_without_reference_images_is_unchanged_two_parts():
    # Regression guard for the four pre-Phase-5 call sites: omitting
    # reference_images must reproduce the original video + text request exactly.
    mock_client_inst = _patched_client(_make_response('{"ok": true}'))

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        call_gemini_for_clip(
            prompt=PROMPT,
            video_gcs_uri=VIDEO_URI,
            start_offset_seconds=10,
            end_offset_seconds=50,
            project_id=PROJECT,
            user_text=USER_TEXT,
        )

    contents = mock_client_inst.models.generate_content.call_args.kwargs["contents"]
    assert len(contents.parts) == 2
    assert contents.parts[0].file_data.file_uri == VIDEO_URI
    assert contents.parts[0].inline_data is None
    assert contents.parts[1].text == USER_TEXT


def test_clip_with_reference_images_labels_each_blob_and_keeps_text_last():
    images = [
        (b"\x01flop", "image/jpeg", "flop"),
        (b"\x02turn", "image/jpeg", "turn"),
        (b"\x03river", "image/png", "river"),
    ]
    mock_client_inst = _patched_client(_make_response('{"ok": true}'))

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        call_gemini_for_clip(
            prompt=PROMPT,
            video_gcs_uri=VIDEO_URI,
            start_offset_seconds=10,
            end_offset_seconds=50,
            project_id=PROJECT,
            user_text=USER_TEXT,
            reference_images=images,
        )

    contents = mock_client_inst.models.generate_content.call_args.kwargs["contents"]
    # video + (label, blob) x 3 + user turn
    assert len(contents.parts) == 8

    # Video first.
    assert contents.parts[0].file_data.file_uri == VIDEO_URI

    # Each blob is immediately preceded by a text part naming it, so the model
    # never has to infer which image is which from arrival order.
    for i, (expected_bytes, expected_mime, expected_label) in enumerate(images):
        label_part = contents.parts[1 + i * 2]
        blob_part = contents.parts[2 + i * 2]
        assert label_part.text == f"Reference image — {expected_label}:"
        assert label_part.inline_data is None
        assert blob_part.inline_data.data == expected_bytes
        assert blob_part.inline_data.mime_type == expected_mime
        assert blob_part.text is None

    # User turn stays last.
    assert contents.parts[7].text == USER_TEXT


def test_reference_image_label_wording_matches_the_scan_prompt():
    """The label binds each image to its description in the scan prompt.

    extract_community_cards.md's STREET VISUAL REFERENCE section names the
    images with this exact string. Rewording either side silently unbinds the
    descriptions from the images, and the resulting degradation would be hard
    to attribute — so the correspondence is asserted rather than commented.
    """
    prompt_text = (
        Path(__file__).resolve().parents[1] / "prompts" / "extract_community_cards.md"
    ).read_text(encoding="utf-8")

    for street in ("flop", "turn", "river"):
        assert f"Reference image — {street}:" in prompt_text


def test_clip_empty_reference_images_list_is_two_parts():
    mock_client_inst = _patched_client(_make_response('{"ok": true}'))

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        call_gemini_for_clip(
            prompt=PROMPT,
            video_gcs_uri=VIDEO_URI,
            start_offset_seconds=10,
            end_offset_seconds=50,
            project_id=PROJECT,
            user_text=USER_TEXT,
            reference_images=[],
        )

    contents = mock_client_inst.models.generate_content.call_args.kwargs["contents"]
    assert len(contents.parts) == 2
    assert contents.parts[1].text == USER_TEXT


def test_clip_user_text_required():
    with pytest.raises(TypeError):
        call_gemini_for_clip(
            prompt=PROMPT,
            video_gcs_uri=VIDEO_URI,
            start_offset_seconds=10,
            end_offset_seconds=50,
            project_id=PROJECT,
        )


def test_frame_user_text_required():
    with pytest.raises(TypeError):
        call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT)


# --- usage / cost instrumentation tests ---


def _call_clip(mock_client_inst, **kwargs):
    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        return call_gemini_for_clip(
            prompt=PROMPT,
            video_gcs_uri=VIDEO_URI,
            start_offset_seconds=10,
            end_offset_seconds=50,
            project_id=PROJECT,
            user_text=USER_TEXT,
            **kwargs,
        )


def test_usage_line_logged_to_stderr_with_label(capsys):
    mock_client_inst = _patched_client(_make_response('{"ok": true}'))

    _call_clip(mock_client_inst, label="step_d")

    line = capsys.readouterr().err.strip()
    assert line.startswith("gemini_usage ")
    assert "model=gemini-2.5-pro" in line
    assert "label=step_d" in line
    assert "prompt_tokens=1200" in line
    assert "candidates_tokens=340" in line
    assert "total_tokens=1540" in line


def test_usage_line_omits_label_when_not_given(capsys):
    mock_client_inst = _patched_client(_make_response('{"ok": true}'))

    _call_clip(mock_client_inst)

    line = capsys.readouterr().err.strip()
    assert line.startswith("gemini_usage ")
    assert "label=" not in line
    assert "total_tokens=1540" in line


def test_usage_logged_for_frame_calls(capsys):
    mock_client_inst = _patched_client(_make_response('{"ok": true}'))

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        call_gemini_for_frame(
            prompt=PROMPT,
            frame_bytes=FRAME_BYTES,
            project_id=PROJECT,
            user_text=USER_TEXT,
            label="card_read_flop",
        )

    line = capsys.readouterr().err.strip()
    assert "label=card_read_flop" in line
    assert "total_tokens=1540" in line


def test_absent_usage_metadata_logs_nothing_and_call_still_succeeds(capsys):
    response = _make_response('{"ok": true}')
    response.usage_metadata = None
    mock_client_inst = _patched_client(response)

    result = _call_clip(mock_client_inst, label="step_d")

    assert result == {"ok": True}
    assert capsys.readouterr().err == ""


def test_partial_usage_metadata_logs_only_present_counts(capsys):
    response = _make_response('{"ok": true}')
    response.usage_metadata = _usage(prompt=900, candidates=None, total=None)
    mock_client_inst = _patched_client(response)

    _call_clip(mock_client_inst)

    line = capsys.readouterr().err.strip()
    assert "prompt_tokens=900" in line
    assert "candidates_tokens" not in line
    assert "total_tokens" not in line


def test_usage_logged_before_parse_failure(capsys):
    # A malformed or truncated response is still a billed call. Logging before
    # validation is what keeps those calls visible in the cost total.
    mock_client_inst = _patched_client(_make_response("not json at all"))

    with pytest.raises(GeminiPermanentError):
        _call_clip(mock_client_inst, label="step_d")

    line = capsys.readouterr().err.strip()
    assert "gemini_usage " in line
    assert "label=step_d" in line
    assert "total_tokens=1540" in line


# --- happy path tests ---


def test_happy_path_returns_dict():
    mock_client_inst = _patched_client(_make_response('{"hand_starts": []}'))

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        result = call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT, user_text=USER_TEXT)

    assert result == {"hand_starts": []}


def test_strips_json_code_fence():
    mock_client_inst = _patched_client(_make_response('```json\n{"x": 1}\n```'))

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        result = call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT, user_text=USER_TEXT)

    assert result == {"x": 1}


def test_strips_bare_code_fence():
    mock_client_inst = _patched_client(_make_response('```\n{"x": 1}\n```'))

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        result = call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT, user_text=USER_TEXT)

    assert result == {"x": 1}


# --- error classification: network / server exceptions ---


@pytest.mark.parametrize("exc", [
    api_exc.ResourceExhausted("rate limited"),
    api_exc.ServiceUnavailable("unavailable"),
    api_exc.DeadlineExceeded("timed out"),
    api_exc.InternalServerError("server error"),
    api_exc.RetryError("retries exhausted", Exception("cause")),
])
def test_transient_exceptions(exc):
    mock_client_inst = MagicMock()
    mock_client_inst.models.generate_content.side_effect = exc

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        with patch("table_talk.gemini_caller.time.sleep"):
            with pytest.raises(GeminiTransientError):
                call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT, user_text=USER_TEXT)


@pytest.mark.parametrize("exc", [
    api_exc.Unauthenticated("not authenticated"),
    api_exc.PermissionDenied("forbidden"),
    api_exc.FailedPrecondition("api not enabled"),
    api_exc.NotFound("model not found"),
    api_exc.InvalidArgument("bad request"),
])
def test_permanent_exceptions(exc):
    mock_client_inst = MagicMock()
    mock_client_inst.models.generate_content.side_effect = exc

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        with pytest.raises(GeminiPermanentError):
            call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT, user_text=USER_TEXT)


# --- error classification: response-level failures ---


def test_max_tokens_finish_reason():
    response = _make_response("partial", finish_reason=types.FinishReason.MAX_TOKENS)
    mock_client_inst = _patched_client(response)

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        with pytest.raises(GeminiPermanentError, match="MAX_TOKENS"):
            call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT, user_text=USER_TEXT)


def test_safety_finish_reason():
    response = _make_response("", finish_reason=types.FinishReason.SAFETY)
    mock_client_inst = _patched_client(response)

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        with pytest.raises(GeminiPermanentError, match="SAFETY"):
            call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT, user_text=USER_TEXT)


def test_empty_text_raises():
    response = _make_response("")
    mock_client_inst = _patched_client(response)

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        with pytest.raises(GeminiPermanentError, match="empty response"):
            call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT, user_text=USER_TEXT)


def test_malformed_json_raises():
    response = _make_response("not valid json {{{")
    mock_client_inst = _patched_client(response)

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        with pytest.raises(GeminiPermanentError, match="malformed JSON"):
            call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT, user_text=USER_TEXT)


# --- retry / backoff tests ---


def test_retry_success_on_first_try():
    mock_client_inst = _patched_client(_make_response('{"ok": true}'))

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        with patch("table_talk.gemini_caller.time.sleep") as mock_sleep:
            result = call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT, user_text=USER_TEXT)

    assert result == {"ok": True}
    mock_sleep.assert_not_called()
    mock_client_inst.models.generate_content.assert_called_once()


@pytest.mark.parametrize("n_failures", [1, 2, 3])
def test_retry_success_after_n_429s(n_failures):
    good_response = _make_response('{"ok": true}')
    side_effects = [api_exc.ResourceExhausted("rate limited")] * n_failures + [good_response]

    mock_client_inst = MagicMock()
    mock_client_inst.models.generate_content.side_effect = side_effects

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        with patch("table_talk.gemini_caller.time.sleep") as mock_sleep:
            result = call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT, user_text=USER_TEXT)

    assert result == {"ok": True}
    assert mock_sleep.call_count == n_failures
    assert mock_client_inst.models.generate_content.call_count == n_failures + 1


def test_retry_exhaustion_raises_transient_with_message():
    mock_client_inst = MagicMock()
    mock_client_inst.models.generate_content.side_effect = api_exc.ResourceExhausted("rate limited")

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        with patch("table_talk.gemini_caller.time.sleep"):
            with pytest.raises(GeminiTransientError, match="retries exhausted"):
                call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT, user_text=USER_TEXT)

    assert mock_client_inst.models.generate_content.call_count == _RETRY_MAX_ATTEMPTS


def test_retry_non_429_transient_not_retried():
    mock_client_inst = MagicMock()
    mock_client_inst.models.generate_content.side_effect = api_exc.ServiceUnavailable("unavailable")

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        with patch("table_talk.gemini_caller.time.sleep") as mock_sleep:
            with pytest.raises(GeminiTransientError):
                call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT, user_text=USER_TEXT)

    mock_sleep.assert_not_called()
    mock_client_inst.models.generate_content.assert_called_once()


def test_retry_permanent_error_not_retried():
    mock_client_inst = MagicMock()
    mock_client_inst.models.generate_content.side_effect = api_exc.PermissionDenied("forbidden")

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        with patch("table_talk.gemini_caller.time.sleep") as mock_sleep:
            with pytest.raises(GeminiPermanentError):
                call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT, user_text=USER_TEXT)

    mock_sleep.assert_not_called()
    mock_client_inst.models.generate_content.assert_called_once()


def test_retry_backoff_timing():
    # 4 failures then success; capture actual sleep values and verify ranges.
    good_response = _make_response('{"ok": true}')
    side_effects = [api_exc.ResourceExhausted("rate limited")] * 4 + [good_response]

    mock_client_inst = MagicMock()
    mock_client_inst.models.generate_content.side_effect = side_effects

    sleep_calls = []

    def capture_sleep(duration):
        sleep_calls.append(duration)

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        with patch("table_talk.gemini_caller.time.sleep", side_effect=capture_sleep):
            call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT, user_text=USER_TEXT)

    assert len(sleep_calls) == 4
    # attempt=0 → [0, 10], attempt=1 → [0, 20], attempt=2 → [0, 40], attempt=3 → [0, 60]
    expected_maxes = [10.0, 20.0, 40.0, 60.0]
    for i, (duration, max_delay) in enumerate(zip(sleep_calls, expected_maxes)):
        assert 0.0 <= duration <= max_delay, (
            f"sleep {i}: expected [0, {max_delay}], got {duration}"
        )


# --- google.genai error family ---
#
# The SDK is google-genai, which raises google.genai.errors.ClientError /
# ServerError. Those share no ancestry with google.api_core.exceptions, so
# every test above that injects api_exc.ResourceExhausted exercises a path the
# real client never takes. That is why the 429 backoff shipped broken and
# stayed broken: the tests were green against the wrong exception family.
#
# These tests inject what the SDK actually raises.


def _genai_error(status, reason="RESOURCE_EXHAUSTED"):
    """An error shaped exactly as google.genai raises it."""
    return genai_errors.ClientError(
        status, {"error": {"code": status, "status": reason, "message": "test"}}
    )


def _failing_client(*side_effects):
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = list(side_effects)
    return mock_client


def test_genai_429_is_retried_then_succeeds():
    """Regression guard for the original bug.

    Against the pre-fix _call_with_retry this fails: the ClientError is not a
    ResourceExhausted, so the except never matches, no sleep happens, and the
    raw ClientError escapes uncaught. If this test ever passes both before and
    after a change to the retry path, it has stopped guarding anything.
    """
    mock_client_inst = _failing_client(_genai_error(429), _make_response('{"ok": true}'))

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        with patch("table_talk.gemini_caller.time.sleep") as mock_sleep:
            result = call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT, user_text=USER_TEXT)

    assert result == {"ok": True}
    mock_sleep.assert_called_once()
    assert mock_client_inst.models.generate_content.call_count == 2


def test_genai_429_exhaustion_raises_transient():
    mock_client_inst = _failing_client(*([_genai_error(429)] * _RETRY_MAX_ATTEMPTS))

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        with patch("table_talk.gemini_caller.time.sleep"):
            with pytest.raises(GeminiTransientError, match="retries exhausted"):
                call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT, user_text=USER_TEXT)

    assert mock_client_inst.models.generate_content.call_count == _RETRY_MAX_ATTEMPTS


def test_genai_400_is_not_retried_and_is_permanent():
    """A non-429 4xx must reach the permanent classification, not the retry.

    Before the fix this escaped uncaught to the orchestrators' catch-all and
    recorded failed_transient — burning the retry cap on something that can
    never succeed, then parking with a status that misrepresents why.
    """
    mock_client_inst = _failing_client(_genai_error(400, "INVALID_ARGUMENT"))

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        with patch("table_talk.gemini_caller.time.sleep") as mock_sleep:
            with pytest.raises(GeminiPermanentError):
                call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT, user_text=USER_TEXT)

    mock_sleep.assert_not_called()
    mock_client_inst.models.generate_content.assert_called_once()


@pytest.mark.parametrize("status", [401, 403, 404])
def test_genai_other_4xx_are_permanent(status):
    mock_client_inst = _failing_client(_genai_error(status, "DENIED"))

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        with patch("table_talk.gemini_caller.time.sleep"):
            with pytest.raises(GeminiPermanentError):
                call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT, user_text=USER_TEXT)


def test_genai_5xx_is_transient_and_not_retried():
    """ServerError already ended up transient via the orchestrators' catch-all.
    It is now classified rather than falling through to it."""
    err = genai_errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE"}})
    mock_client_inst = _failing_client(err)

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        with patch("table_talk.gemini_caller.time.sleep") as mock_sleep:
            with pytest.raises(GeminiTransientError):
                call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT, user_text=USER_TEXT)

    mock_sleep.assert_not_called()
    mock_client_inst.models.generate_content.assert_called_once()


def test_genai_429_retried_on_clip_caller_too():
    """Both callers share _call_with_retry; neither may regress alone."""
    mock_client_inst = _failing_client(_genai_error(429), _make_response('{"ok": true}'))

    with patch("table_talk.gemini_caller.time.sleep") as mock_sleep:
        result = _call_clip(mock_client_inst)

    assert result == {"ok": True}
    mock_sleep.assert_called_once()


# --- status extraction and rate-limit predicate ---


class _StatusCodeOnlyError(genai_errors.APIError):
    """A genai error exposing `status_code` instead of `code`.

    google-genai 2.7.0 exposes `code`, but the spelling has moved across
    versions, so the extractor tries both. No object in the installed SDK
    reaches that branch, hence this stub.
    """

    def __init__(self, status_code):
        self.status_code = status_code


def test_genai_status_code_reads_code_attribute():
    assert _genai_status_code(_genai_error(429)) == 429
    assert _genai_status_code(_genai_error(400, "INVALID_ARGUMENT")) == 400


def test_genai_status_code_falls_back_to_status_code_attribute():
    assert _genai_status_code(_StatusCodeOnlyError(429)) == 429


def test_genai_status_code_returns_none_when_unreadable():
    assert _genai_status_code(Exception("no status here")) is None

    non_int = _genai_error(429)
    non_int.code = "429"  # a string, not an int — must not be trusted
    assert _genai_status_code(non_int) is None


def test_unreadable_genai_status_classifies_transient():
    """Matches the orchestrators' 'anything not recognised is transient' rule:
    an unclassifiable failure must stay retryable rather than be discarded."""
    unreadable = _genai_error(429)
    unreadable.code = None
    assert isinstance(_classify_genai_error(unreadable), GeminiTransientError)


@pytest.mark.parametrize("exc,expected", [
    (api_exc.ResourceExhausted("rate limited"), True),
    (_genai_error(429), True),
    (_genai_error(400, "INVALID_ARGUMENT"), False),
    (api_exc.PermissionDenied("forbidden"), False),
])
def test_is_rate_limited(exc, expected):
    assert _is_rate_limited(exc) is expected


def test_rate_limit_detection_does_not_read_the_message():
    """A message mentioning 429 for an unrelated reason must not be retried —
    substring matching would turn a permanent failure into three wasted calls."""
    misleading = _genai_error(400, "INVALID_ARGUMENT")
    misleading.args = ("400 INVALID_ARGUMENT. token budget 429 exceeded",)
    assert _is_rate_limited(misleading) is False


# --- integration test ---


@pytest.mark.integration
def test_call_gemini_for_frame_integration():
    with tempfile.TemporaryDirectory() as tmpdir:
        img_path = os.path.join(tmpdir, "frame.jpg")
        subprocess.run(
            [
                "ffmpeg", "-f", "lavfi",
                "-i", "color=black:size=64x64:duration=1",
                "-frames:v", "1", "-y", img_path,
            ],
            check=True,
            capture_output=True,
        )
        with open(img_path, "rb") as f:
            img_bytes = f.read()

    instruction = (
        "You return ONLY a single JSON object with one key 'status' set to 'ok'. "
        "No code fences, no preamble."
    )
    result = call_gemini_for_frame(
        prompt=instruction,
        frame_bytes=img_bytes,
        project_id="table-talk-497020",
        location="global",
        user_text="Extract the status from this frame.",
    )

    assert isinstance(result, dict)
    assert len(result) > 0
