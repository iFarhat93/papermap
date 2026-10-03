"""OpenAI-compatible chat completions: OpenAI, vLLM, llama.cpp server, LM Studio,
OpenRouter, Groq, Together, ... (anything exposing ``POST /chat/completions``)."""

from __future__ import annotations

import os

import httpx

from .base import LLMError, LLMProvider, LLMRequest, LLMResponse, TransientLLMError

# preset -> (default base_url, default api key env var, supports json_object, supports json_schema)
PRESETS: dict[str, tuple[str | None, str | None, bool, bool]] = {
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY", True, False),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", True, False),
    "vllm": ("http://localhost:8000/v1", None, True, True),
    "llamacpp": ("http://localhost:8080/v1", None, True, True),
    "lmstudio": ("http://localhost:1234/v1", None, False, True),
    "openai_compatible": (None, None, False, False),
}


class OpenAICompatibleProvider(LLMProvider):
    name = "openai"

    def __init__(self, settings, preset: str = "openai"):
        super().__init__(settings)
        base, key_env, json_ok, schema_ok = PRESETS.get(preset, PRESETS["openai_compatible"])
        self.name = preset
        self.base_url = (settings.base_url or base or "").rstrip("/")
        if not self.base_url:
            raise LLMError(f"provider {preset!r} needs llm.base_url (e.g. http://localhost:8000/v1)")
        self.key_env = settings.api_key_env or key_env
        self.api_key = os.environ.get(self.key_env) if self.key_env else None
        if self.key_env and not self.api_key and preset in ("openai", "openrouter"):
            raise LLMError(f"environment variable {self.key_env} is not set (needed for provider {preset!r})")
        self.supports_json_mode = json_ok
        self.supports_schema = schema_ok

    def fingerprint(self):
        fp = super().fingerprint()
        fp["base_url"] = self.base_url
        return fp

    def complete(self, request: LLMRequest) -> LLMResponse:
        messages = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages += [{"role": m.role, "content": m.content} for m in request.flattened()]
        body: dict = {"model": self.settings.model, "messages": messages, "max_tokens": request.max_tokens}
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.seed is not None:
            body["seed"] = request.seed
        mode = self.structured_mode()
        if request.json_schema is not None and mode == "schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": request.json_schema, "strict": False},
            }
        elif request.json_mode and mode in ("json", "schema"):
            body["response_format"] = {"type": "json_object"}
        body.update(self.settings.extra)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            r = httpx.post(f"{self.base_url}/chat/completions", json=body, headers=headers, timeout=self.settings.timeout)
        except httpx.ConnectError as e:
            raise LLMError(f"cannot reach {self.base_url}: {e}") from e
        except httpx.TimeoutException as e:
            raise TransientLLMError(f"timeout after {self.settings.timeout}s") from e
        except httpx.HTTPError as e:
            raise TransientLLMError(str(e)) from e
        if r.status_code == 429 or r.status_code >= 500:
            raise TransientLLMError(f"HTTP {r.status_code}: {r.text[:300]}")
        if r.status_code >= 400:
            raise LLMError(f"HTTP {r.status_code} from {self.base_url}: {r.text[:500]}")
        data = r.json()
        try:
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"unexpected response shape: {str(data)[:300]}") from e
        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            usage={"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0)},
            model=data.get("model", self.settings.model),
            truncated=choice.get("finish_reason") == "length",
        )
