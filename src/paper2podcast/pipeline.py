"""Pipeline orchestration:

Paper -> parse -> understand -> profile -> explain -> diagrams -> graph -> narrate -> render

Every cacheable stage is keyed by (stage name, version, source hash of its
module, keys of its dependencies, relevant config). A cached stage is loaded
instead of recomputed; a failed run resumes from the last finished LLM call.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import sys
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from .config import stable_hash
from .log import stage_logger
from .models import SCHEMA_VERSION, Experience
from .stages import diagrams, explain, graph, narrate, parse, profile, render, understand
from .stages.base import RunContext, Stage

STAGES: list[Stage] = [
    parse.STAGE,
    understand.STAGE,
    profile.STAGE,
    explain.STAGE,
    diagrams.STAGE,
    graph.STAGE,
    narrate.STAGE,
    render.STAGE,
]
STAGE_NAMES = [s.name for s in STAGES]


class PipelineError(RuntimeError):
    def __init__(self, stage: str, cause: BaseException):
        super().__init__(f"stage '{stage}' failed: {cause}")
        self.stage = stage
        self.cause = cause


@dataclass
class RunResult:
    experience: Experience | None
    outputs: dict[str, Any]
    timings: dict[str, float] = field(default_factory=dict)
    cached: list[str] = field(default_factory=list)


@lru_cache(maxsize=1)
def _package_source_hash() -> str:
    """Hash of every Python module in the package. Any code change (prompts, grounding,
    post-processing) invalidates stage outputs; unchanged LLM calls still hit the call
    cache, so recomputing is cheap and results never silently go stale."""
    root = Path(__file__).parent
    h = hashlib.sha256()
    for p in sorted(root.rglob("*.py")):
        h.update(p.relative_to(root).as_posix().encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def _source_hash(stage: Stage) -> str:
    if stage.name == "parse":  # parsing must not depend on unrelated code edits beyond its module
        module = sys.modules.get(stage.run.__module__)
        try:
            return stable_hash(inspect.getsource(module))[:16] if module else ""
        except (OSError, TypeError):
            return ""
    return _package_source_hash()


def stage_key(stage: Stage, ctx: RunContext, keys: dict[str, str]) -> str:
    llm = None
    if stage.uses_llm:
        llm = ctx.config.llm_for(stage.name).model_dump(mode="json")
        for volatile in ("timeout", "max_retries"):
            llm.pop(volatile, None)
    return stable_hash(
        stage.name, stage.version, SCHEMA_VERSION, _source_hash(stage),
        [keys[d] for d in stage.deps], llm, stage.key_extra(ctx),
    )


def run_pipeline(
    ctx: RunContext,
    *,
    force: Iterable[str] = (),
    until: str | None = None,
) -> RunResult:
    force = set(force)
    unknown = force - set(STAGE_NAMES)
    if unknown:
        raise ValueError(f"unknown stage(s) {sorted(unknown)}; stages are: {', '.join(STAGE_NAMES)}")
    if until and until not in STAGE_NAMES:
        raise ValueError(f"unknown stage {until!r}; stages are: {', '.join(STAGE_NAMES)}")

    outputs: dict[str, Any] = {}
    keys: dict[str, str] = {}
    result = RunResult(experience=None, outputs=outputs)
    ctx.stage_keys = keys
    debug_dir = ctx.out_dir / "debug"

    for stage in STAGES:
        log = stage_logger(stage.name)
        key = stage_key(stage, ctx, keys)
        keys[stage.name] = key
        data = None
        if stage.cacheable and stage.name not in force:
            data = ctx.cache.get_stage(stage.name, key)
        t0 = time.perf_counter()
        if data is not None:
            try:
                outputs[stage.name] = stage.output.model_validate(data)
                result.cached.append(stage.name)
                log.info("cached")
            except Exception:  # stale or corrupt cache entry: recompute
                data = None
        if data is None:
            deps = {d: outputs[d] for d in stage.deps}
            try:
                out = stage.run(ctx, deps)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                raise PipelineError(stage.name, e) from e
            outputs[stage.name] = out
            if stage.cacheable:
                ctx.cache.put_stage(stage.name, key, out.model_dump(mode="json"))
            result.timings[stage.name] = time.perf_counter() - t0
            log.debug("done in %.1fs", result.timings[stage.name])
        if stage.name != "render":
            debug_dir.mkdir(parents=True, exist_ok=True)
            (debug_dir / f"{stage.name}.json").write_text(
                json.dumps(outputs[stage.name].model_dump(mode="json"), ensure_ascii=False, indent=1), "utf-8"
            )
        if until and stage.name == until:
            break

    result.experience = outputs.get("render")
    return result
