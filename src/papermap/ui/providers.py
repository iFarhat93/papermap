"""Providers as the web UI sees them: which need a key, where keys come from,
which models a server offers, prices and rough estimates."""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from ..config import LLMSettings
from ..llm.openai_compat import PRESETS

# provider -> (label, needs a key, runs on this machine, default base URL)
PROVIDERS: dict[str, dict[str, Any]] = {
    "anthropic": {"label": "Anthropic", "key": True, "local": False, "base_url": None},
    "openai": {"label": "OpenAI", "key": True, "local": False, "base_url": PRESETS["openai"][0]},
    "openrouter": {"label": "OpenRouter", "key": True, "local": False, "base_url": PRESETS["openrouter"][0]},
    "ollama": {"label": "Ollama", "key": False, "local": True, "base_url": "http://localhost:11434"},
    "vllm": {"label": "vLLM", "key": False, "local": True, "base_url": PRESETS["vllm"][0]},
    "lmstudio": {"label": "LM Studio", "key": False, "local": True, "base_url": PRESETS["lmstudio"][0]},
    "llamacpp": {"label": "llama.cpp", "key": False, "local": True, "base_url": PRESETS["llamacpp"][0]},
    "openai_compatible": {"label": "Other OpenAI-compatible", "key": False, "local": False, "base_url": None},
    "mock": {"label": "Mock (offline demo)", "key": False, "local": False, "base_url": None},
}

# Where each provider reads its key. Servers without a conventional variable use PAPERMAP_API_KEY.
KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "openrouter": "OPENROUTER_API_KEY"}
GENERIC_KEY_ENV = "PAPERMAP_API_KEY"
KEYRING_SERVICE = "papermap"

# Models people commonly pick, shown before (or without) a live model list.
SUGGESTED = {
    "anthropic": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-haiku-4-5"],
    "openai": ["gpt-6.1-sol", "gpt-4.1"],
    "openrouter": ["anthropic/claude-sonnet-5-5", "anthropic/claude-opus-5-5"],
    "ollama": ["qwen3.8:27b", "qwen2.5:7b"],
    "mock": ["mock"],
}
TESTED = {"qwen3.8:27b", "claude-sonnet-5-5", "claude-opus-5-5", "gpt-6.1-sol"}

# USD per million tokens (input, output), first-party API list prices.
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

# Measured on "Attention Is All You Need" (15 pages, see README): tokens and minutes per page.
TOKENS_PER_PAGE = (16_000, 2_100)
MINUTES_PER_PAGE = {"cloud": 0.35, "local": 2.7}
DEFAULT_PAGES = 15


# What people paste when they copy a full endpoint instead of the base URL.
_ENDPOINT_SUFFIXES = ("/chat/completions", "/completions", "/messages", "/models", "/api/tags", "/api/chat")


def clean_base_url(provider: str, url: str | None) -> str | None:
    """Base URL as each client expects it: ".../v1" for OpenAI-compatible servers,
    the bare host for Anthropic (its SDK adds /v1/messages itself)."""
    url = (url or "").strip().rstrip("/")
    changed = True
    while url and changed:
        changed = False
        for suffix in _ENDPOINT_SUFFIXES:
            if url.endswith(suffix):
                url = url[: -len(suffix)].rstrip("/")
                changed = True
    if provider == "anthropic":
        url = url.removesuffix("/v1")
    return url or None


def is_local(provider: str, base_url: str | None = None) -> bool:
    info = PROVIDERS.get(provider, {})
    if info.get("local"):
        return True
    url = (base_url or "").lower()
    return any(h in url for h in ("://localhost", "://127.0.0.1", "://[::1]", "://0.0.0.0"))


def key_env_for(provider: str) -> str | None:
    if provider in KEY_ENV:
        return KEY_ENV[provider]
    if provider in ("mock", "ollama"):
        return None
    return GENERIC_KEY_ENV


def price_for(provider: str, model: str) -> tuple[float, float] | None:
    """(input, output) USD per million tokens; (0, 0) for local models; None when unknown."""
    if is_local(provider) or provider == "mock":
        return (0.0, 0.0)
    name = model.split("/", 1)[-1] if provider == "openrouter" else model
    for known in sorted(PRICES, key=len, reverse=True):  # longest first: claude-opus-5-5 before claude-opus-5
        if name == known or name.startswith(known + "-"):
            return PRICES[known]
    return None


def cost(provider: str, model: str, input_tokens: int, output_tokens: int) -> float | None:
    p = price_for(provider, model)
    if p is None:
        return None
    return (input_tokens * p[0] + output_tokens * p[1]) / 1e6


def estimate(provider: str, model: str, base_url: str | None, pages: int | None) -> dict[str, Any]:
    n = pages or DEFAULT_PAGES
    lane = "local" if is_local(provider, base_url) else "cloud"
    tin, tout = TOKENS_PER_PAGE[0] * n, TOKENS_PER_PAGE[1] * n
    return {
        "pages": n, "pages_known": pages is not None, "minutes": round(MINUTES_PER_PAGE[lane] * n, 1),
        "cost": cost(provider, model, tin, tout), "local": lane == "local",
    }


# ----------------------------------------------------------------------- keys


class KeyStore:
    """API keys live in this process's environment (and so reach every run it starts).
    "Remember" also saves them in the OS keychain through the optional `keyring` package."""

    def __init__(self) -> None:
        self.source: dict[str, str] = {}
        for env in {*KEY_ENV.values(), GENERIC_KEY_ENV}:
            if os.environ.get(env):
                self.source[env] = "environment"
            else:
                saved = self._keyring_get(env)
                if saved:
                    os.environ[env] = saved
                    self.source[env] = "keychain"

    @staticmethod
    def keyring_available() -> bool:
        try:
            import keyring  # noqa: F401
        except ImportError:
            return False
        return True

    @staticmethod
    def _keyring_get(env: str) -> str | None:
        try:
            import keyring

            return keyring.get_password(KEYRING_SERVICE, env)
        except Exception:  # no keyring package or no usable backend
            return None

    def status(self, env: str | None) -> dict[str, Any]:
        if not env:
            return {"env": None, "set": False, "source": None}
        value = os.environ.get(env) or ""
        return {"env": env, "set": bool(value), "source": self.source.get(env) if value else None,
                "hint": f"…{value[-4:]}" if len(value) >= 12 else ("set" if value else "")}

    def set(self, env: str, value: str, remember: bool) -> str:
        """Returns a warning, or "" when everything worked."""
        value = value.strip()
        if not value:
            return "empty key"
        os.environ[env] = value
        self.source[env] = "entered"
        if not remember:
            return ""
        try:
            import keyring

            keyring.set_password(KEYRING_SERVICE, env, value)
            self.source[env] = "keychain"
            return ""
        except ImportError:
            return 'the key is used for this session only: install keyring (pip install "papermap[ui]") to remember it'
        except Exception as e:
            return f"the key is used for this session only: the keychain refused it ({e})"

    def forget(self, env: str) -> None:
        os.environ.pop(env, None)
        self.source.pop(env, None)
        try:
            import keyring

            keyring.delete_password(KEYRING_SERVICE, env)
        except Exception:
            pass


# --------------------------------------------------------------------- models


def list_models(provider: str, base_url: str | None) -> tuple[list[str], str]:
    """Models the provider offers, and an error message when the list could not be fetched."""
    suggested = SUGGESTED.get(provider, [])
    base_url = clean_base_url(provider, base_url)
    base = (base_url or PROVIDERS.get(provider, {}).get("base_url") or "").rstrip("/")
    try:
        if provider == "mock":
            return ["mock"], ""
        if provider == "anthropic":
            try:
                import anthropic
            except ImportError:
                return suggested, 'install the SDK to list models: pip install "papermap[anthropic]"'
            kwargs: dict[str, Any] = {"max_retries": 0, "timeout": 15.0}
            if base_url:
                kwargs["base_url"] = base_url
            client = anthropic.Anthropic(**kwargs)
            ids = [m.id for m in client.models.list(limit=100)]
            ids = [i for i in ids if i.startswith("claude")] or ids  # gateways may list other vendors' models too
            return _merge(ids, suggested), ""
        if provider == "ollama":
            root = base.removesuffix("/v1")
            r = httpx.get(f"{root}/api/tags", timeout=8)
            r.raise_for_status()
            return sorted(m["name"] for m in r.json().get("models", [])), ""
        if not base:
            return suggested, "set the server URL first"
        env = key_env_for(provider)
        headers = {"Authorization": f"Bearer {os.environ[env]}"} if env and os.environ.get(env) else {}
        r = httpx.get(f"{base}/models", headers=headers, timeout=15)
        r.raise_for_status()
        # keep the server's order: proxies and gateways list their default model first
        ids = [m["id"] for m in r.json().get("data", []) if isinstance(m, dict) and m.get("id")]
        if provider == "openai":
            ids = [i for i in ids if i.startswith(("gpt-", "o1", "o3", "o4", "chatgpt"))] or ids
        return _merge(ids, suggested if provider == "openai" else []), ""
    except Exception as e:  # any network/auth problem becomes a message in the UI
        return suggested, _short_error(e)


def _merge(ids: list[str], suggested: list[str]) -> list[str]:
    return [m for m in suggested if m in ids or not ids] + [m for m in ids if m not in suggested]


def _short_error(e: Exception) -> str:
    if isinstance(e, httpx.ConnectError):
        return "could not reach the server - is it running?"
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        return "the server rejected the API key" if code in (401, 403) else f"the server answered HTTP {code}"
    name = type(e).__name__
    text = str(e) or name
    if name == "AuthenticationError" or "authentication" in text.lower() or "api key" in text.lower() and "not set" in text.lower():
        return "add a valid API key first"
    if name == "APIConnectionError":
        return "could not reach the server - check your connection"
    return text[:300]


def test_connection(settings: LLMSettings) -> dict[str, Any]:
    from ..llm import create_provider

    t0 = time.perf_counter()
    try:
        provider = create_provider(settings.model_copy(update={"timeout": 90.0, "max_retries": 0}))
        reply = provider.check()
    except Exception as e:  # report any failure to the page
        return {"ok": False, "error": _short_error(e)}
    return {"ok": True, "seconds": round(time.perf_counter() - t0, 1), "reply": reply}
