"""Model layer. Swap backends by changing ``[llm] provider`` in the config.

Built-in providers:
  anthropic                                   Claude via the official SDK
  ollama                                      local, native Ollama API
  openai | vllm | llamacpp | lmstudio |       any OpenAI-compatible server
  openrouter | openai_compatible
  mock                                        deterministic offline provider (tests/demo)

Third-party backends can register themselves with :func:`register_provider`
or through the ``papermap.providers`` entry-point group.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Callable

from ..config import LLMSettings
from .base import LLMError, LLMProvider, LLMRequest, LLMResponse, Message, TransientLLMError
from .client import LLMClient, LLMOutputError
from .openai_compat import PRESETS

ProviderFactory = Callable[[LLMSettings], LLMProvider]
_FACTORIES: dict[str, ProviderFactory] = {}


def register_provider(name: str, factory: ProviderFactory) -> None:
    _FACTORIES[name.lower()] = factory


def available_providers() -> list[str]:
    return sorted({"anthropic", "ollama", "mock", *PRESETS, *_FACTORIES})


def create_provider(settings: LLMSettings) -> LLMProvider:
    name = settings.provider.lower().strip()
    if name in _FACTORIES:
        return _FACTORIES[name](settings)
    if name == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(settings)
    if name == "ollama":
        from .ollama import OllamaProvider

        return OllamaProvider(settings)
    if name == "mock":
        from .mock import MockProvider

        return MockProvider(settings)
    if name in PRESETS:
        from .openai_compat import OpenAICompatibleProvider

        return OpenAICompatibleProvider(settings, preset=name)
    for ep in entry_points(group="papermap.providers"):
        if ep.name == name:
            return ep.load()(settings)
    raise LLMError(f"unknown LLM provider {settings.provider!r}; choose one of: {', '.join(available_providers())}")


__all__ = [
    "LLMClient",
    "LLMError",
    "LLMOutputError",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "Message",
    "TransientLLMError",
    "available_providers",
    "create_provider",
    "register_provider",
]
