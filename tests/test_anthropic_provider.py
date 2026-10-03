"""Request construction for the Anthropic provider, with the SDK client stubbed out."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

anthropic = pytest.importorskip("anthropic")

from papermap.config import LLMSettings  # noqa: E402
from papermap.llm import LLMError  # noqa: E402
from papermap.llm.anthropic_provider import AnthropicProvider  # noqa: E402
from papermap.llm.base import LLMRequest, Message  # noqa: E402


class _Stream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


def _message(text="{}", stop="end_turn"):
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=7, cache_creation_input_tokens=0)
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop, usage=usage, model="claude-opus-5-5", stop_details=None)


@pytest.fixture()
def provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    p = AnthropicProvider(LLMSettings(provider="anthropic", model="claude-opus-5-5", effort="medium"))
    calls: dict = {}

    def beta_stream(**kw):
        calls["beta"] = kw
        return _Stream(calls.get("reply", _message('{"ok": true}')))

    def plain_stream(**kw):
        calls["plain"] = kw
        return _Stream(calls.get("reply", _message('{"ok": true}')))

    p.client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=beta_stream)),
                               messages=SimpleNamespace(stream=plain_stream))
    return p, calls


def test_request_uses_fallbacks_effort_and_cached_context(provider):
    p, calls = provider
    resp = p.complete(LLMRequest(messages=[Message("user", "TASK: x")], system="sys", context="SECTION TEXT", temperature=0.2, max_tokens=900))
    kw = calls["beta"]
    assert kw["betas"] == ["server-side-fallback-2026-07-01"] and kw["fallbacks"] == "default"
    assert kw["output_config"] == {"effort": "medium"}
    assert kw["system"] == "sys" and kw["max_tokens"] == 900
    assert "temperature" not in kw  # rejected by current Claude models
    first = kw["messages"][0]["content"]
    assert first[0]["text"] == "SECTION TEXT" and first[0]["cache_control"] == {"type": "ephemeral"}
    assert first[1]["text"] == "TASK: x"
    assert resp.text == '{"ok": true}' and resp.usage["cache_read_tokens"] == 7


def test_models_without_fallback_support_use_the_plain_endpoint(provider, monkeypatch):
    p, calls = provider
    p.settings = p.settings.model_copy(update={"model": "claude-haiku-4-5"})
    p.complete(LLMRequest(messages=[Message("user", "hi")]))
    assert "plain" in calls and "fallbacks" not in calls["plain"]


def test_refusal_is_reported(provider):
    p, calls = provider
    calls["reply"] = _message("", stop="refusal")
    with pytest.raises(LLMError, match="declined"):
        p.complete(LLMRequest(messages=[Message("user", "hi")]))
