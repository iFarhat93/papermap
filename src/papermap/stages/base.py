"""Stage framework: a stage is a named, versioned function from its
dependencies' outputs to one pydantic model, cached by content hash."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

from pydantic import BaseModel

from ..cache import Cache
from ..config import Config
from ..llm import LLMClient, create_provider
from ..log import stage_logger

T = TypeVar("T")
R = TypeVar("R")

# One system prompt for every stage keeps the request prefix identical across
# stages, which lets prefix caching (Anthropic, vLLM, llama.cpp) reuse it.
SYSTEM_PROMPT = """You are the writing engine of PaperMap. It turns a scientific paper into a short, visual, narrated learning experience for one specific person.

Ground rules:
- Use only information stated in the provided paper text. Never invent results, numbers, names, datasets, equations or claims. If something is not in the text, leave it out.
- Be precise and concise. One sharp sentence beats three loose ones.
- Output exactly what the task asks for. When JSON is requested, return one JSON object and nothing else."""


@dataclass
class RunContext:
    config: Config
    cache: Cache
    out_dir: Path
    source: str
    profile_text: str
    stage_keys: dict[str, str] = field(default_factory=dict)
    _clients: dict[str, LLMClient] = field(default_factory=dict)

    def llm(self, stage: str) -> LLMClient:
        if stage not in self._clients:
            settings = self.config.llm_for(stage)
            # stages that share identical settings share one client (and usage counter)
            for name, client in self._clients.items():
                if client.settings == settings:
                    self._clients[stage] = client
                    break
            else:
                self._clients[stage] = LLMClient(create_provider(settings), self.cache)
        return self._clients[stage]

    def clients(self) -> list[LLMClient]:
        seen: list[LLMClient] = []
        for c in self._clients.values():
            if all(c is not s for s in seen):
                seen.append(c)
        return seen

    def parallel(self, fn: Callable[[T], R], items: Iterable[T]) -> list[R]:
        """Order-preserving parallel map; the first exception propagates (completed
        LLM calls are cached, so a re-run resumes where this one stopped)."""
        items = list(items)
        workers = max(1, min(self.config.pipeline.concurrency, len(items) or 1))
        if workers == 1:
            return [fn(x) for x in items]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(fn, items))


@dataclass
class Stage:
    name: str
    version: str
    deps: tuple[str, ...]
    output: type[BaseModel]
    run: Callable[[RunContext, dict[str, Any]], BaseModel]
    uses_llm: bool = True
    cacheable: bool = True
    # Extra inputs that should invalidate this stage's cache when they change.
    key_extra: Callable[[RunContext], Any] = lambda ctx: None
    description: str = ""

    def log(self):
        return stage_logger(self.name)
