# Vertex AI Gemini caller for Phase 3 clip and frame analysis.
# Stateless primitive — creates a fresh client per call, no shared state.
# Error classification mirrors google.api_core.exceptions HTTP semantics.

import json
import re

import google.api_core.exceptions as api_exc
from google import genai
from google.genai import types


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
) -> dict:
    client = genai.Client(vertexai=True, project=project_id, location=location)

    part1 = types.Part(
        file_data=types.FileData(file_uri=video_gcs_uri, mime_type="video/*"),
        video_metadata=types.VideoMetadata(
            start_offset=f"{start_offset_seconds}s",
            end_offset=f"{end_offset_seconds}s",
            fps=1.0,
        ),
    )
    part2 = types.Part(text="Identify all new hand setups in this video.")
    request_contents = types.Content(role="user", parts=[part1, part2])

    try:
        response = client.models.generate_content(
            model="gemini-2.5-pro",
            config=types.GenerateContentConfig(system_instruction=prompt),
            contents=request_contents,
        )
    except _TRANSIENT_EXC as exc:
        raise GeminiTransientError(str(exc)) from exc
    except _PERMANENT_EXC as exc:
        raise GeminiPermanentError(str(exc)) from exc

    return _parse_and_validate(response)


def call_gemini_for_frame(
    prompt: str,
    frame_bytes: bytes,
    project_id: str,
    location: str = "global",
    mime_type: str = "image/jpeg",
) -> dict:
    client = genai.Client(vertexai=True, project=project_id, location=location)

    part1 = types.Part(inline_data=types.Blob(data=frame_bytes, mime_type=mime_type))
    part2 = types.Part(text="Extract the setup observations from this frame.")
    request_contents = types.Content(role="user", parts=[part1, part2])

    try:
        response = client.models.generate_content(
            model="gemini-2.5-pro",
            config=types.GenerateContentConfig(
                system_instruction=prompt,
                media_resolution=types.MediaResolution.MEDIA_RESOLUTION_HIGH,
            ),
            contents=request_contents,
        )
    except _TRANSIENT_EXC as exc:
        raise GeminiTransientError(str(exc)) from exc
    except _PERMANENT_EXC as exc:
        raise GeminiPermanentError(str(exc)) from exc

    return _parse_and_validate(response)
