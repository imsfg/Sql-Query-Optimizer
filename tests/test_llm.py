"""Provider selection and the Gemini adapter (SDK client replaced by a stub; no network)."""

from types import SimpleNamespace

import pytest
from google.genai import errors

from app.config import Settings
from app.llm import AnthropicLLM, DebugResult, GeminiLLM, IntentDecision, LLMError, inline_schema_refs, make_llm


def test_provider_selection(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    s = Settings()
    assert s.llm_provider == "gemini" and s.llm_model.startswith("gemini-")
    assert isinstance(make_llm(s), GeminiLLM)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    s = Settings()
    assert s.llm_provider == "anthropic" and s.llm_model == "claude-opus-5"
    assert isinstance(make_llm(s), AnthropicLLM)

    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("LLM_MODEL", "gemini-2.5-flash")
    assert Settings().llm_model == "gemini-2.5-flash"
    monkeypatch.setenv("LLM_MODEL", "claude-opus-5")  # leftover from the other provider
    assert Settings().llm_model == "gemini-3.8-flash"

    monkeypatch.setenv("LLM_PROVIDER", "nope")
    with pytest.raises(ValueError):
        Settings()


def test_inline_schema_refs():
    schema = inline_schema_refs(DebugResult.model_json_schema())
    assert "$defs" not in str(schema) and "$ref" not in str(schema)
    assert schema["properties"]["issues"]["items"]["properties"]["why"]["type"] == "string"


class StubModels:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def gemini_with(outcome, model="gemini-3.8-flash"):
    llm = GeminiLLM(Settings(llm_provider="gemini", llm_model=model))
    stub = StubModels(outcome)
    llm._client = SimpleNamespace(models=stub)
    return llm, stub


def response(text):
    return SimpleNamespace(text=text, candidates=[], usage_metadata=None)


def ask(llm, effort="low"):
    return llm.generate(system="SYS", prompt="hello", schema=IntentDecision, effort=effort, task="classify")


def test_gemini_structured_output_and_config():
    llm, stub = gemini_with(response('{"intent": "generate", "user_sql": null, "reason": "data question"}'))
    out = ask(llm, effort="medium")
    assert out.intent == "generate" and out.user_sql is None
    config = stub.calls[0]["config"]
    assert config.system_instruction == "SYS" and config.response_mime_type == "application/json"
    assert config.thinking_config.thinking_level.value == "MEDIUM"


def test_gemini_2_uses_thinking_budget():
    llm, stub = gemini_with(response('{"intent": "help", "reason": "hi"}'), model="gemini-2.5-flash")
    ask(llm)
    assert stub.calls[0]["config"].thinking_config.thinking_budget == 512


@pytest.mark.parametrize("error, expected", [
    (errors.ClientError(429, {"error": {"message": "Resource exhausted", "status": "RESOURCE_EXHAUSTED"}}), "quota"),
    (errors.ClientError(400, {"error": {"message": "API key not valid.", "status": "INVALID_ARGUMENT"}}), "API key"),
    (errors.ClientError(404, {"error": {"message": "not found", "status": "NOT_FOUND"}}), "not found"),
    (errors.ServerError(503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}}), "overloaded"),
    (errors.ServerError(500, {"error": {"message": "internal", "status": "INTERNAL"}}), "service returned an error"),
    (ConnectionError("boom"), "Could not reach"),
])
def test_gemini_errors_become_user_messages(error, expected):
    llm, _ = gemini_with(error)
    with pytest.raises(LLMError, match=expected):
        ask(llm)


def test_gemini_falls_back_to_second_model_when_overloaded():
    llm, _ = gemini_with(None)
    ok = response('{"intent": "help", "reason": "hi"}')
    outcomes = iter([errors.ServerError(503, {"error": {"message": "busy", "status": "UNAVAILABLE"}}), ok])

    class Seq:
        calls = []

        def generate_content(self, *, model, contents, config):
            self.calls.append(model)
            item = next(outcomes)
            if isinstance(item, Exception):
                raise item
            return item

    seq = Seq()
    llm._client = SimpleNamespace(models=seq)
    assert ask(llm).intent == "help"
    assert seq.calls == ["gemini-3.8-flash", "gemini-3.7-flash"]


def test_gemini_invalid_json_and_empty_answer():
    llm, _ = gemini_with(response('{"intent": "not-an-intent", "reason": "x"}'))
    with pytest.raises(LLMError, match="could not be parsed"):
        ask(llm)
    llm, _ = gemini_with(SimpleNamespace(text=None, candidates=[SimpleNamespace(finish_reason="SAFETY")],
                                         usage_metadata=None))
    with pytest.raises(LLMError, match="SAFETY"):
        ask(llm)


def test_gemini_missing_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    llm = GeminiLLM(Settings(llm_provider="gemini"))
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        ask(llm)
