from __future__ import annotations

from typing import Protocol

from google import genai
from google.genai import types


class LLMClient(Protocol):
    def complete(
        self,
        system_prompt: str,
        user_message: str,
        response_schema: type | None = None,
    ) -> str: ...


class StubLLMClient:
    """Deterministic test double. Maps (system_prompt, user_message) pairs to canned responses."""

    def __init__(self, responses: dict[tuple[str, str], str]) -> None:
        self._responses = responses

    def complete(
        self,
        system_prompt: str,
        user_message: str,
        response_schema: type | None = None,
    ) -> str:
        key = (system_prompt, user_message)
        if key not in self._responses:
            raise KeyError(
                f"No canned response registered for this prompt pair. "
                f"System: {system_prompt[:80]!r} | User: {user_message[:80]!r}"
            )
        return self._responses[key]


class GeminiLLMClient:
    """Live Gemini client via google-genai SDK."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gemini-2.5-flash",
    ) -> None:
        # api_key=None → SDK reads GOOGLE_API_KEY / GEMINI_API_KEY from environment
        self._client = genai.Client(api_key=api_key)
        self._model = model

    def complete(
        self,
        system_prompt: str,
        user_message: str,
        response_schema: type | None = None,
    ) -> str:
        if response_schema is not None:
            config = types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                response_schema=response_schema,
            )
        else:
            config = types.GenerateContentConfig(
                system_instruction=system_prompt,
            )
        response = self._client.models.generate_content(
            model=self._model,
            contents=user_message,
            config=config,
        )
        return response.text
