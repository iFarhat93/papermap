"""Pipeline orchestration:

Paper -> parse -> understand -> profile -> explain -> review -> diagrams -> graph -> quiz -> narrate -> render

Every cacheable stage is keyed by (stage name, version, source hash of its
module, keys of its dependencies, relevant config). A cached stage is loaded
instead of recomputed; a failed run resumes from the last finished LLM call.

Progress goes to ``<out>/logs/status.json`` after every stage (the web UI reads it),
and the cache entries a run used are listed in ``<out>/logs/cache_files.json``.
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

from .cache import _atomic_write
from .config import stable_hash
from .log import stage_logger
from .models import SCHEMA_VERSION, Experience
from .stages import diagrams, explain, graph, narrate, parse, profile, quiz, render, review, understand
from .stages.base import RunContext, Stage

STAGES: list[Stage] = [
    parse.STAGE,
    understand.STAGE,
    profile.STAGE,
    explain.STAGE,
    review.STAGE,
    diagrams.STAGE,
    graph.STAGE,
    quiz.STAGE,
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
    if stage.name == "parse":  # parsing depends only on its own module and the paper-source readers
        module = sys.modules.get(stage.run.__module__)
        root = Path(__file__).parent
        try:
            parts = [inspect.getsource(module)] if module else []
            parts += [p.read_text("utf-8") for p in sorted((root / "sources").glob("*.py"))]
            return stable_hash(parts)[:16]
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


class _Status:
    """Machine-readable progress for the web UI: current stage, finished stages, usage."""

    def __init__(self, ctx: RunContext):
        self.ctx = ctx
        self.path = ctx.out_dir / "logs" / "status.json"
        self.data: dict[str, Any] = {
            "state": "running", "source": ctx.source, "stages": STAGE_NAMES, "current": None,
            "done": [], "cached": [], "error": None, "failed_stage": None, "started": time.time(),
        }

    def update(self, **fields: Any) -> None:
        self.data.update(fields)
        self.data["updated"] = time.time()
        self.data["usage"] = [
            {"provider": c.provider.name, "model": c.settings.model, "calls": c.usage.calls, "cached": c.usage.cached,
             "input_tokens": c.usage.input_tokens, "output_tokens": c.usage.output_tokens, "seconds": round(c.usage.seconds, 1)}
            for c in self.ctx.clients()
        ]
        try:
            _atomic_write(self.path, json.dumps(self.data, ensure_ascii=False, indent=1).encode("utf-8"))
        except OSError:  # progress reporting must never break a run
            pass


def _write_cache_manifest(ctx: RunContext) -> None:
    """Union of the cache entries this output folder has used across its runs."""
    path = ctx.out_dir / "logs" / "cache_files.json"
    try:
        old = set(json.loads(path.read_text("utf-8"))) if path.is_file() else set()
    except (OSError, ValueError):
        old = set()
    files = sorted(old | ctx.cache.touched)
    try:
        _atomic_write(path, json.dumps(files, indent=0).encode("utf-8"))
    except OSError:
        pass


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

    status = _Status(ctx)
    status.update()
    try:
        result = _run_stages(ctx, force, until, status)
    except PipelineError as e:
        status.update(state="failed", error=str(e.cause), failed_stage=e.stage, current=None)
        raise
    except BaseException as e:
        status.update(state="interrupted" if isinstance(e, KeyboardInterrupt) else "failed", error=str(e) or type(e).__name__, current=None)
        raise
    finally:
        _write_cache_manifest(ctx)
    status.update(state="done", current=None)
    return result


def _run_stages(ctx: RunContext, force: set[str], until: str | None, status: _Status) -> RunResult:
    outputs: dict[str, Any] = {}
    keys: dict[str, str] = {}
    result = RunResult(experience=None, outputs=outputs)
    ctx.stage_keys = keys
    debug_dir = ctx.out_dir / "debug"

    for stage in STAGES:
        status.update(current=stage.name)
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
        status.data["done"].append(stage.name)
        status.data["cached"] = list(result.cached)
        status.update()
        if until and stage.name == until:
            break

    result.experience = outputs.get("render")
    return result
