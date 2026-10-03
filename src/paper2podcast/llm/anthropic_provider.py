"""Claude via the official Anthropic SDK (``pip install "paper2podcast[anthropic]"``).

Credentials resolve the SDK's usual way (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN,
or an ``ant auth login`` profile) unless ``llm.api_key_env`` names another variable.
The section-text ``context`` block is marked for prompt caching, so the several
stages that read the same section reuse it.
"""

from __future__ import annotations

import os

from .base import LLMError, LLMProvider, LLMRequest, LLMResponse, TransientLLMError

# Models that accept the server-side refusal fallback in its "default" form.
_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, settings):
        super().__init__(settings)
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover - depends on extras
            raise LLMError('the anthropic provider needs the SDK: pip install "paper2podcast[anthropic]"') from e
        self._sdk = anthropic
        kwargs: dict = {"max_retries": settings.max_retries, "timeout": settings.timeout}
        if settings.api_key_env:
            key = os.environ.get(settings.api_key_env)
            if not key:
                raise LLMError(f"environment variable {settings.api_key_env} is not set")
            kwargs["api_key"] = key
        if settings.base_url:
            kwargs["base_url"] = settings.base_url
        try:
            self.client = anthropic.Anthropic(**kwargs)
        except anthropic.AnthropicError as e:
            raise LLMError(f"could not create Anthropic client: {e}") from e

    def complete(self, request: LLMRequest) -> LLMResponse:
        sdk = self._sdk
        messages: list[dict] = []
        for i, m in enumerate(request.messages):
            if i == 0 and request.context and m.role == "user":
                content = [
                    {"type": "text", "text": request.context, "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": m.content},
                ]
                messages.append({"role": "user", "content": content})
            else:
                messages.append({"role": m.role, "content": m.content})
        params: dict = {"model": self.settings.model, "max_tokens": request.max_tokens, "messages": messages}
        if request.system:
            params["system"] = request.system
        if self.settings.effort:
            params["output_config"] = {"effort": self.settings.effort}
        params.update(self.settings.extra)
        use_fallbacks = self.settings.fallbacks and self.settings.model in _FALLBACK_MODELS and not self.settings.base_url
        try:
            if use_fallbacks:
                stream = self.client.beta.messages.stream(betas=[_FALLBACK_BETA], fallbacks="default", **params)
            else:
                stream = self.client.messages.stream(**params)
            with stream as s:
                msg = s.get_final_message()
        except sdk.AuthenticationError as e:
            raise LLMError("Anthropic authentication failed: set ANTHROPIC_API_KEY or run `ant auth login`") from e
        except sdk.NotFoundError as e:
            raise LLMError(f"Anthropic model {self.settings.model!r} not found: {e.message}") from e
        except sdk.BadRequestError as e:
            raise LLMError(f"Anthropic rejected the request: {e.message}") from e
        except sdk.RateLimitError as e:
            raise TransientLLMError("Anthropic rate limit") from e
        except sdk.APIStatusError as e:
            if e.status_code >= 500:
                raise TransientLLMError(f"Anthropic server error {e.status_code}") from e
            raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except sdk.APIConnectionError as e:
            raise TransientLLMError(f"connection error: {e}") from e
        except TypeError as e:  # the SDK raises TypeError when no credential source resolves
            if "authentication" in str(e).lower():
                raise LLMError(
                    "no Anthropic credentials found: set ANTHROPIC_API_KEY (or llm.api_key_env), or run `ant auth login`"
                ) from e
            raise

        if msg.stop_reason == "refusal":
            details = getattr(msg, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise LLMError(f"the model declined this request (category: {category})")
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        u = msg.usage
        usage = {
            "input_tokens": (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0)
            + (getattr(u, "cache_creation_input_tokens", 0) or 0),
            "output_tokens": u.output_tokens or 0,
            "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        }
        return LLMResponse(text=text, usage=usage, model=msg.model, truncated=msg.stop_reason == "max_tokens")
