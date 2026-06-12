import os
import subprocess
import tempfile
from unittest.mock import MagicMock, call, patch

import google.api_core.exceptions as api_exc
import pytest
from google.genai import types

from table_talk.gemini_caller import (
    GeminiPermanentError,
    GeminiTransientError,
    _RETRY_MAX_ATTEMPTS,
    call_gemini_for_clip,
    call_gemini_for_frame,
)

PROJECT = "test-project"
PROMPT = "You are a poker analyst."
VIDEO_URI = "gs://bucket/video.mp4"
FRAME_BYTES = b"\xff\xd8\xff" + b"\x00" * 20  # fake bytes — mocked, not parsed


def _make_response(text: str, finish_reason=types.FinishReason.STOP):
    candidate = MagicMock()
    candidate.finish_reason = finish_reason
    response = MagicMock()
    response.text = text
    response.candidates = [candidate]
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

    assert contents.parts[1].text == "Identify all new hand setups in this video."


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
        )

    mock_cls.assert_called_once_with(vertexai=True, project=PROJECT, location="us-east1")

    kw = mock_client_inst.models.generate_content.call_args.kwargs
    assert kw["model"] == "gemini-2.5-pro"

    contents = kw["contents"]
    assert contents.parts[0].inline_data.data == custom_bytes
    assert contents.parts[0].inline_data.mime_type == "image/png"
    assert contents.parts[1].text == "Extract the setup observations from this frame."

    assert kw["config"].media_resolution == types.MediaResolution.MEDIA_RESOLUTION_HIGH


# --- happy path tests ---


def test_happy_path_returns_dict():
    mock_client_inst = _patched_client(_make_response('{"hand_starts": []}'))

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        result = call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT)

    assert result == {"hand_starts": []}


def test_strips_json_code_fence():
    mock_client_inst = _patched_client(_make_response('```json\n{"x": 1}\n```'))

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        result = call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT)

    assert result == {"x": 1}


def test_strips_bare_code_fence():
    mock_client_inst = _patched_client(_make_response('```\n{"x": 1}\n```'))

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        result = call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT)

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
                call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT)


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
            call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT)


# --- error classification: response-level failures ---


def test_max_tokens_finish_reason():
    response = _make_response("partial", finish_reason=types.FinishReason.MAX_TOKENS)
    mock_client_inst = _patched_client(response)

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        with pytest.raises(GeminiPermanentError, match="MAX_TOKENS"):
            call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT)


def test_safety_finish_reason():
    response = _make_response("", finish_reason=types.FinishReason.SAFETY)
    mock_client_inst = _patched_client(response)

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        with pytest.raises(GeminiPermanentError, match="SAFETY"):
            call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT)


def test_empty_text_raises():
    response = _make_response("")
    mock_client_inst = _patched_client(response)

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        with pytest.raises(GeminiPermanentError, match="empty response"):
            call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT)


def test_malformed_json_raises():
    response = _make_response("not valid json {{{")
    mock_client_inst = _patched_client(response)

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        with pytest.raises(GeminiPermanentError, match="malformed JSON"):
            call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT)


# --- retry / backoff tests ---


def test_retry_success_on_first_try():
    mock_client_inst = _patched_client(_make_response('{"ok": true}'))

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        with patch("table_talk.gemini_caller.time.sleep") as mock_sleep:
            result = call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT)

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
            result = call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT)

    assert result == {"ok": True}
    assert mock_sleep.call_count == n_failures
    assert mock_client_inst.models.generate_content.call_count == n_failures + 1


def test_retry_exhaustion_raises_transient_with_message():
    mock_client_inst = MagicMock()
    mock_client_inst.models.generate_content.side_effect = api_exc.ResourceExhausted("rate limited")

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        with patch("table_talk.gemini_caller.time.sleep"):
            with pytest.raises(GeminiTransientError, match="retries exhausted"):
                call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT)

    assert mock_client_inst.models.generate_content.call_count == _RETRY_MAX_ATTEMPTS


def test_retry_non_429_transient_not_retried():
    mock_client_inst = MagicMock()
    mock_client_inst.models.generate_content.side_effect = api_exc.ServiceUnavailable("unavailable")

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        with patch("table_talk.gemini_caller.time.sleep") as mock_sleep:
            with pytest.raises(GeminiTransientError):
                call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT)

    mock_sleep.assert_not_called()
    mock_client_inst.models.generate_content.assert_called_once()


def test_retry_permanent_error_not_retried():
    mock_client_inst = MagicMock()
    mock_client_inst.models.generate_content.side_effect = api_exc.PermissionDenied("forbidden")

    with patch("table_talk.gemini_caller.genai.Client", return_value=mock_client_inst):
        with patch("table_talk.gemini_caller.time.sleep") as mock_sleep:
            with pytest.raises(GeminiPermanentError):
                call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT)

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
            call_gemini_for_frame(PROMPT, FRAME_BYTES, PROJECT)

    assert len(sleep_calls) == 4
    # attempt=0 → [0, 10], attempt=1 → [0, 20], attempt=2 → [0, 40], attempt=3 → [0, 60]
    expected_maxes = [10.0, 20.0, 40.0, 60.0]
    for i, (duration, max_delay) in enumerate(zip(sleep_calls, expected_maxes)):
        assert 0.0 <= duration <= max_delay, (
            f"sleep {i}: expected [0, {max_delay}], got {duration}"
        )


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
    )

    assert isinstance(result, dict)
    assert len(result) > 0
