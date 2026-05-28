import pytest

from table_talk.insights.clients.llm import GeminiLLMClient, StubLLMClient


def test_stub_returns_registered_response():
    stub = StubLLMClient({("sys", "usr"): "pong"})
    assert stub.complete("sys", "usr") == "pong"


def test_stub_response_schema_arg_ignored():
    stub = StubLLMClient({("sys", "usr"): '{"x": 1}'})
    assert stub.complete("sys", "usr", response_schema=dict) == '{"x": 1}'


def test_stub_raises_key_error_on_missing_pair():
    stub = StubLLMClient({("sys", "usr"): "pong"})
    with pytest.raises(KeyError, match="No canned response"):
        stub.complete("other-sys", "other-usr")


def test_stub_key_error_message_includes_prompts():
    stub = StubLLMClient({})
    with pytest.raises(KeyError, match="my-system"):
        stub.complete("my-system", "my-user")


def test_gemini_instantiates_without_live_call():
    # Verifies the constructor stores state without making any network request.
    client = GeminiLLMClient(project="dummy-project-not-used")
    assert client is not None
    assert client._model == "gemini-2.5-flash"


def test_gemini_accepts_custom_model():
    client = GeminiLLMClient(project="dummy-project-not-used", model="gemini-2.0-flash")
    assert client._model == "gemini-2.0-flash"
