"""Provider interface: the only thing a model backend has to implement.

A provider turns one :class:`LLMRequest` into one :class:`LLMResponse`.
Caching, retries, JSON extraction/validation and logging live in
:class:`papermap.llm.client.LLMClient`, so providers stay tiny.
"""

from __future__ import annotations

import copy
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from ..config import LLMSettings


class LLMError(RuntimeError):
    """A failure that retrying will not fix (bad key, unknown model, refusal...)."""


class TransientLLMError(LLMError):
    """A failure worth retrying (timeouts, rate limits, 5xx)."""


@dataclass
class Message:
    role: Literal["user", "assistant"]
    content: str


@dataclass
class LLMRequest:
    messages: list[Message]
    system: str | None = None
    # Large, stable context (e.g. section text). Placed before the first user
    # message so providers with prefix caching can reuse it across stages.
    context: str | None = None
    temperature: float | None = None
    max_tokens: int = 4096
    seed: int | None = None
    json_mode: bool = False
    json_schema: dict[str, Any] | None = None
    tag: str = ""  # task label for logs; not part of the cache key

    def cache_payload(self) -> dict[str, Any]:
        return {
            "messages": [(m.role, m.content) for m in self.messages],
            "system": self.system,
            "context": self.context,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "seed": self.seed,
            "json_mode": self.json_mode,
            "json_schema": self.json_schema,
        }

    def flattened(self) -> list[Message]:
        """Messages with ``context`` merged into the first user turn."""
        msgs = [Message(m.role, m.content) for m in self.messages]
        if self.context and msgs:
            msgs[0] = Message(msgs[0].role, f"{self.context}\n\n{msgs[0].content}")
        return msgs


@dataclass
class LLMResponse:
    text: str
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""
    truncated: bool = False


class LLMProvider(ABC):
    """Base class for model backends."""

    name: str = "base"
    supports_schema: bool = False
    supports_json_mode: bool = False

    def __init__(self, settings: LLMSettings):
        self.settings = settings

    @abstractmethod
    def complete(self, request: LLMRequest) -> LLMResponse:  # pragma: no cover - interface
        ...

    def fingerprint(self) -> dict[str, Any]:
        """Identity used in cache keys: anything that changes the model's output."""
        return {
            "provider": self.name,
            "model": self.settings.model,
            "base_url": self.settings.base_url,
            "extra": self.settings.extra,
            "effort": getattr(self.settings, "effort", None) if self.name == "anthropic" else None,
        }

    def structured_mode(self) -> str:
        mode = (self.settings.structured_output or "auto").lower()
        if mode == "auto":
            return "schema" if self.supports_schema else "json" if self.supports_json_mode else "none"
        if mode == "schema" and not self.supports_schema:
            return "json" if self.supports_json_mode else "none"
        if mode == "json" and not self.supports_json_mode:
            return "none"
        return mode

    def check(self) -> str:
        """Cheap connectivity check used by ``papermap check``."""
        resp = self.complete(LLMRequest(messages=[Message("user", "Reply with the single word: ok")], max_tokens=200))
        return resp.text.strip()[:80]


def inline_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve local ``$ref``s and drop titles: some servers' grammar
    converters only understand flat schemas."""
    schema = copy.deepcopy(schema)
    defs = schema.pop("$defs", {}) or schema.pop("definitions", {})

    def resolve(node: Any, depth: int = 0, is_properties: bool = False) -> Any:
        if depth > 20:
            return {}
        if isinstance(node, dict):
            if is_properties:  # keys are field names (a field may well be called "title")
                return {k: resolve(v, depth + 1) for k, v in node.items()}
            if "$ref" in node:
                name = node["$ref"].split("/")[-1]
                target = defs.get(name, {})
                merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
                return resolve(merged, depth + 1)
            out = {
                k: resolve(v, depth + 1, is_properties=(k == "properties"))
                for k, v in node.items()
                if k not in ("title", "default")  # annotations, not fields
            }
            # Constrained decoders only emit optional keys when they feel like it;
            # requiring every key yields complete objects from small local models.
            if isinstance(out.get("properties"), dict):
                out["required"] = list(out["properties"])
            return out
        if isinstance(node, list):
            return [resolve(v, depth + 1) for v in node]
        return node

    return resolve(schema)
