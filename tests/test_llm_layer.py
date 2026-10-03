from __future__ import annotations

import pytest
from pydantic import BaseModel

from papermap.cache import Cache
from papermap.config import LLMSettings
from papermap.llm import LLMClient, LLMOutputError, create_provider
from papermap.llm.base import LLMProvider, LLMRequest, LLMResponse, TransientLLMError, inline_json_schema
from papermap.llm.jsonutil import extract_json


class Out(BaseModel):
    title: str
    items: list[str]


class Scripted(LLMProvider):
    """Returns canned responses in order and records requests."""

    name = "scripted"

    def __init__(self, responses):
        super().__init__(LLMSettings(provider="scripted", model="s", max_retries=2))
        self.responses = list(responses)
        self.requests: list[LLMRequest] = []

    def complete(self, request):
        self.requests.append(request)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return LLMResponse(text=r, usage={"input_tokens": 1, "output_tokens": 1})


def test_extract_json_variants():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('<think>hmm {"no": 1}</think>Here: {"a": [1, 2,],}') == {"a": [1, 2]}
    assert extract_json('prefix {"a": "b"} suffix') == {"a": "b"}


def test_complete_json_feeds_errors_back_and_retries(tmp_path):
    p = Scripted(['{"title": "x"}', '{"title": "x", "items": ["a"]}'])
    client = LLMClient(p, Cache(tmp_path))
    out = client.complete_json("TASK: t", Out, tag="t")
    assert out.items == ["a"]
    assert len(p.requests) == 2
    fix = p.requests[1].messages[-1].content
    assert "items" in fix and "problems" in fix


def test_semantic_check_failure_raises_after_retries(tmp_path):
    p = Scripted(['{"title": "x", "items": []}'] * 3)
    client = LLMClient(p, Cache(tmp_path))
    with pytest.raises(LLMOutputError):
        client.complete_json("TASK: t", Out, check=lambda o: [] if o.items else ["items must not be empty"], retries=2)


def test_llm_calls_are_cached(tmp_path):
    cache = Cache(tmp_path)
    p1 = Scripted(['{"title": "x", "items": ["a"]}'])
    LLMClient(p1, cache).complete_json("TASK: t", Out)
    p2 = Scripted([])  # would fail if called
    client = LLMClient(p2, cache)
    p2.settings = p1.settings
    out = client.complete_json("TASK: t", Out)
    assert out.title == "x" and client.usage.cached == 1


def test_transient_errors_are_retried(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    p = Scripted([TransientLLMError("503"), "hello"])
    assert LLMClient(p, None).complete("hi") == "hello"


def test_inline_schema_keeps_fields_named_title_and_requires_all():
    s = inline_json_schema(Out.model_json_schema())
    assert set(s["properties"]) == {"title", "items"}
    assert set(s["required"]) == {"title", "items"}
    assert "title" not in s or s.get("title") is None


def test_provider_registry():
    assert create_provider(LLMSettings(provider="mock", model="m")).name == "mock"
    assert create_provider(LLMSettings(provider="ollama", model="m")).name == "ollama"
    assert create_provider(LLMSettings(provider="vllm", model="m")).base_url == "http://localhost:8000/v1"
    with pytest.raises(Exception):
        create_provider(LLMSettings(provider="nope", model="m"))
