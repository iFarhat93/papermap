"""LLMClient: caching, retries, JSON validation and accounting around a provider."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from pydantic import BaseModel, ValidationError

from ..cache import Cache
from ..config import stable_hash
from ..log import get_logger
from .base import LLMError, LLMProvider, LLMRequest, Message, TransientLLMError, inline_json_schema
from .jsonutil import JSONExtractionError, extract_json, strip_reasoning, summarize_validation_error

T = TypeVar("T", bound=BaseModel)

log = get_logger("llm")


class LLMOutputError(LLMError):
    def __init__(self, tag: str, problems: list[str]):
        super().__init__(f"{tag}: model output still invalid after retries: {'; '.join(problems[:5])}")
        self.problems = problems


@dataclass
class Usage:
    calls: int = 0
    cached: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0

    def summary(self) -> str:
        return (
            f"{self.calls} LLM calls ({self.cached} from cache), "
            f"{self.input_tokens:,} input / {self.output_tokens:,} output tokens, {self.seconds:.0f}s in model calls"
        )


FIX_PROMPT = (
    "Your previous answer could not be used because of these problems:\n{problems}\n\n"
    "Return the complete corrected JSON object only, with no commentary."
)


class LLMClient:
    def __init__(self, provider: LLMProvider, cache: Cache | None = None):
        self.provider = provider
        self.settings = provider.settings
        self.cache = cache
        self.usage = Usage()
        self._lock = threading.Lock()

    # ------------------------------------------------------------- raw text
    def complete(
        self,
        prompt: str | list[Message],
        *,
        system: str | None = None,
        context: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        json_schema: dict | None = None,
        json_mode: bool = False,
        tag: str = "",
    ) -> str:
        messages = [Message("user", prompt)] if isinstance(prompt, str) else list(prompt)
        req = LLMRequest(
            messages=messages,
            system=system,
            context=context,
            temperature=self.settings.temperature if temperature is None else temperature,
            max_tokens=max_tokens or self.settings.max_tokens,
            seed=self.settings.seed,
            json_mode=json_mode,
            json_schema=json_schema,
            tag=tag,
        )
        key = stable_hash(self.provider.fingerprint(), req.cache_payload())
        if self.cache:
            hit = self.cache.get_llm(key)
            if hit is not None:
                with self._lock:
                    self.usage.calls += 1
                    self.usage.cached += 1
                log.debug("cache hit %s (%s)", tag, key[:10])
                return hit

        resp = self._call_with_retries(req)
        text = strip_reasoning(resp.text)
        if resp.truncated:
            log.warning("%s: output hit max_tokens=%d and may be cut off", tag or "llm", req.max_tokens)
        if self.cache:
            self.cache.put_llm(
                key,
                {"tag": tag, **self.provider.fingerprint(), **req.cache_payload()},
                text,
                {"usage": resp.usage, "model": resp.model},
            )
        return text

    def _call_with_retries(self, req: LLMRequest):
        attempts = max(1, self.settings.max_retries + 1)
        delay = 2.0
        for i in range(attempts):
            t0 = time.perf_counter()
            try:
                resp = self.provider.complete(req)
            except TransientLLMError as e:
                if i == attempts - 1:
                    raise
                log.warning("%s: transient error (%s); retrying in %.0fs", req.tag or "llm", e, delay)
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            dt = time.perf_counter() - t0
            with self._lock:
                self.usage.calls += 1
                self.usage.seconds += dt
                self.usage.input_tokens += int(resp.usage.get("input_tokens", 0))
                self.usage.output_tokens += int(resp.usage.get("output_tokens", 0))
            log.debug(
                "%s: %s/%s %.1fs in=%s out=%s",
                req.tag or "llm",
                self.provider.name,
                self.settings.model,
                dt,
                resp.usage.get("input_tokens", "?"),
                resp.usage.get("output_tokens", "?"),
            )
            return resp
        raise LLMError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------ structured
    def complete_json(
        self,
        prompt: str,
        schema: type[T],
        *,
        system: str | None = None,
        context: str | None = None,
        check: Callable[[T], list[str]] | None = None,
        retries: int = 2,
        max_tokens: int | None = None,
        tag: str = "",
    ) -> T:
        """Ask for JSON matching ``schema``; feed validation problems back to the
        model and retry. ``check`` adds semantic validation (e.g. id integrity)."""
        mode = self.provider.structured_mode()
        json_schema = inline_json_schema(schema.model_json_schema()) if mode == "schema" else None
        messages = [Message("user", prompt)]
        problems: list[str] = []
        for attempt in range(retries + 1):
            text = self.complete(
                messages,
                system=system,
                context=context,
                max_tokens=max_tokens,
                json_schema=json_schema,
                json_mode=mode in ("json", "schema"),
                tag=tag if attempt == 0 else f"{tag}#fix{attempt}",
            )
            try:
                obj = extract_json(text)
                if isinstance(obj, list) and len(schema.model_fields) == 1:
                    # model returned the bare list for a single-field wrapper
                    obj = {next(iter(schema.model_fields)): obj}
                result = schema.model_validate(obj)
                problems = check(result) if check else []
            except JSONExtractionError as e:
                problems = [f"output was not valid JSON ({e})"]
            except ValidationError as e:
                problems = summarize_validation_error(e)
            if not problems:
                return result
            log.warning("%s: invalid output (attempt %d/%d): %s", tag, attempt + 1, retries + 1, "; ".join(problems[:3]))
            messages = messages + [
                Message("assistant", text[:12000]),
                Message("user", FIX_PROMPT.format(problems="\n".join(f"- {p}" for p in problems[:12]))),
            ]
        raise LLMOutputError(tag, problems)
