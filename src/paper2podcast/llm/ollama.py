"""Ollama's native API (``/api/chat``): lets us set ``num_ctx`` and use
schema-constrained decoding, which matters a lot for small local models."""

from __future__ import annotations

import httpx

from .base import LLMError, LLMProvider, LLMRequest, LLMResponse, TransientLLMError


class OllamaProvider(LLMProvider):
    name = "ollama"
    supports_schema = True
    supports_json_mode = True

    def __init__(self, settings):
        super().__init__(settings)
        self.base_url = (settings.base_url or "http://localhost:11434").rstrip("/")
        if self.base_url.endswith("/v1"):
            self.base_url = self.base_url[:-3]

    def fingerprint(self):
        fp = super().fingerprint()
        fp["base_url"] = self.base_url
        fp["num_ctx"] = self.settings.num_ctx
        fp["think"] = self.settings.think
        return fp

    def complete(self, request: LLMRequest) -> LLMResponse:
        messages = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages += [{"role": m.role, "content": m.content} for m in request.flattened()]
        options: dict = {"num_predict": request.max_tokens}
        if request.temperature is not None:
            options["temperature"] = request.temperature
        if request.seed is not None:
            options["seed"] = request.seed
        if self.settings.num_ctx:
            options["num_ctx"] = self.settings.num_ctx
        body: dict = {"model": self.settings.model, "messages": messages, "stream": False, "options": options}
        mode = self.structured_mode()
        if request.json_schema is not None and mode == "schema":
            body["format"] = request.json_schema
        elif request.json_mode and mode in ("json", "schema"):
            body["format"] = "json"
        if self.settings.think is not None:
            body["think"] = self.settings.think
        body.update(self.settings.extra)
        try:
            r = httpx.post(f"{self.base_url}/api/chat", json=body, timeout=self.settings.timeout)
        except httpx.ConnectError as e:
            raise LLMError(f"cannot reach Ollama at {self.base_url} - is `ollama serve` running? ({e})") from e
        except httpx.TimeoutException as e:
            raise TransientLLMError(f"timeout after {self.settings.timeout}s") from e
        except httpx.HTTPError as e:
            raise TransientLLMError(str(e)) from e
        if r.status_code == 404:
            raise LLMError(f"Ollama model {self.settings.model!r} not found - run `ollama pull {self.settings.model}`")
        if r.status_code >= 500 or r.status_code == 429:
            raise TransientLLMError(f"HTTP {r.status_code}: {r.text[:300]}")
        if r.status_code >= 400:
            raise LLMError(f"HTTP {r.status_code} from Ollama: {r.text[:500]}")
        data = r.json()
        text = (data.get("message") or {}).get("content") or ""
        return LLMResponse(
            text=text,
            usage={"input_tokens": data.get("prompt_eval_count", 0), "output_tokens": data.get("eval_count", 0)},
            model=data.get("model", self.settings.model),
            truncated=data.get("done_reason") == "length",
        )
