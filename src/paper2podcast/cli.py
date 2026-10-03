"""Command line interface.

    paper2podcast paper.pdf --profile profile.md          # = paper2podcast run ...
    paper2podcast serve p2p-out/<name>                     # open it, with grounded Q&A
    paper2podcast init                                     # write config + profile templates
    paper2podcast check                                    # test the model/TTS configuration
    paper2podcast stages                                   # list pipeline stages
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .cache import Cache
from .config import EXAMPLE_CONFIG, Config, load_config
from .log import add_file_handler, get_logger, setup_logging

COMMANDS = ("run", "serve", "init", "check", "stages")

EXAMPLE_PROFILE = """# Who is listening?

Write freely - paper2podcast reads this file to adapt the explanation.

- **Name:** Alex
- **Background:** software engineer, 6 years of backend work; comfortable with Python and basic linear algebra.
- **Knows well:** neural networks at a conceptual level, gradient descent, APIs, databases.
- **New to:** attention mechanisms, transformers, research-paper notation.
- **Interests:** practical takeaways - what this changes for real systems, cost and efficiency.
- **Preferred depth:** balanced - intuition first, then the key technical details; skip proofs.
- **Style:** friendly and direct; analogies from software engineering welcome.
- **Language:** English
"""


def default_cache_dir() -> Path:
    env = os.environ.get("P2P_CACHE_DIR")
    if env:
        return Path(env)
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "paper2podcast" / "cache"
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "paper2podcast"


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9.]+", "-", text).strip("-")[:60] or "paper"


def default_out_dir(source: str, profile: str | None) -> Path:
    p = Path(source)
    if p.suffix.lower() == ".pdf":
        name = p.stem
    else:
        m = re.search(r"(\d{4}\.\d{4,5})", source)
        name = m.group(1) if m else source.rstrip("/").split("/")[-1]
    if profile:
        name = f"{name}--{Path(profile).stem}"
    return Path("p2p-out") / _slug(name)


def _add_model_flags(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("model")
    g.add_argument("-c", "--config", help="path to paper2podcast.toml")
    g.add_argument("--provider", help="LLM provider: anthropic, ollama, openai, vllm, llamacpp, lmstudio, openrouter, openai_compatible, mock")
    g.add_argument("--model", help="model name for the provider")
    g.add_argument("--base-url", help="API base URL (local servers, proxies)")
    g.add_argument("--api-key-env", help="environment variable holding the API key")


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    o: dict[str, Any] = {}
    llm = {k: v for k, v in {
        "provider": getattr(args, "provider", None),
        "model": getattr(args, "model", None),
        "base_url": getattr(args, "base_url", None),
        "api_key_env": getattr(args, "api_key_env", None),
    }.items() if v}
    if llm.get("provider") and not llm.get("model"):
        llm["model"] = {"ollama": "qwen2.5:7b", "openai": "gpt-4.1", "anthropic": "claude-opus-5-5", "mock": "mock"}.get(llm["provider"], "")
    if llm:
        o["llm"] = llm
    tts = {k: v for k, v in {"provider": getattr(args, "tts", None)}.items() if v}
    if tts:
        o["tts"] = tts
    pipe = {k: v for k, v in {
        "narration_style": getattr(args, "style", None),
        "concurrency": getattr(args, "concurrency", None),
    }.items() if v}
    if pipe:
        o["pipeline"] = pipe
    return o


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paper2podcast",
        description="Turn a scientific paper into an interactive, audience-adapted, podcast-style webpage.",
    )
    parser.add_argument("--version", action="version", version=f"paper2podcast {__version__}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="generate the experience for a paper (default command)")
    run.add_argument("paper", help="PDF path, PDF URL, arXiv URL or arXiv id")
    run.add_argument("-p", "--profile", help="profile.md describing the listener")
    run.add_argument("-o", "--out", help="output directory (default: p2p-out/<paper>-<profile>)")
    _add_model_flags(run)
    run.add_argument("--tts", help="narration: browser (default), none, openai, edge")
    run.add_argument("--style", choices=["narrator", "duo"], help="one narrator or a host+expert duo")
    run.add_argument("--concurrency", type=int, help="parallel LLM calls (default 4)")
    run.add_argument("--until", metavar="STAGE", help="stop after this stage")
    run.add_argument("--force", metavar="STAGE", action="append", default=[], help="recompute a stage even if cached (repeatable)")
    run.add_argument("--no-cache", action="store_true", help="ignore all caches (fresh LLM calls)")
    run.add_argument("--cache-dir", help=f"cache location (default: {default_cache_dir()})")
    run.add_argument("--serve", action="store_true", help="serve the result with Q&A when done")
    run.add_argument("--port", type=int, default=8765)
    run.add_argument("-v", "--verbose", action="store_true", help="debug output")
    run.add_argument("-q", "--quiet", action="store_true", help="warnings and errors only")

    srv = sub.add_parser("serve", help="serve a generated experience with grounded Q&A")
    srv.add_argument("dir", help="output directory produced by `run`")
    _add_model_flags(srv)
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8765)
    srv.add_argument("--no-browser", action="store_true")
    srv.add_argument("--no-qa", action="store_true", help="static serving only")
    srv.add_argument("--cache-dir")
    srv.add_argument("-v", "--verbose", action="store_true")

    init = sub.add_parser("init", help="write paper2podcast.toml and profile.md templates")
    init.add_argument("--dir", default=".", help="where to write them")
    init.add_argument("--force", action="store_true", help="overwrite existing files")

    chk = sub.add_parser("check", help="check that the configured model (and TTS) respond")
    _add_model_flags(chk)
    chk.add_argument("--tts", help="also test this TTS provider")
    chk.add_argument("-v", "--verbose", action="store_true")

    sub.add_parser("stages", help="list the pipeline stages")
    return parser


def _normalize_argv(argv: list[str]) -> list[str]:
    if not argv:
        return argv
    first = argv[0]
    if first in COMMANDS or first in ("-h", "--help", "--version"):
        return argv
    return ["run", *argv]


# ------------------------------------------------------------------- commands


def cmd_run(args: argparse.Namespace) -> int:
    from .pipeline import PipelineError, run_pipeline
    from .stages.base import RunContext

    log = setup_logging(args.verbose, args.quiet)
    config, found = load_config(args.config, _overrides(args))
    out_dir = Path(args.out) if args.out else default_out_dir(args.paper, args.profile)
    out_dir.mkdir(parents=True, exist_ok=True)
    add_file_handler(out_dir / "logs" / "run.log")
    profile_text = ""
    if args.profile:
        pf = Path(args.profile)
        if not pf.is_file():
            log.error("profile not found: %s", pf)
            return 2
        profile_text = pf.read_text("utf-8")
    else:
        log.warning("no --profile given; using a generic curious-reader profile")
    cache = Cache(args.cache_dir or default_cache_dir(), enabled=not args.no_cache)
    log.info(
        "paper2podcast %s | model %s/%s | tts %s | config %s",
        __version__, config.llm.provider, config.llm.model, config.tts.provider, found or "defaults",
    )
    log.debug("cache: %s | output: %s", cache.root, out_dir.resolve())
    ctx = RunContext(config=config, cache=cache, out_dir=out_dir, source=args.paper, profile_text=profile_text)
    t0 = time.perf_counter()
    try:
        result = run_pipeline(ctx, force=args.force, until=args.until)
    except PipelineError as e:
        log.error("%s", e)
        log.error("completed work is cached - re-run the same command to resume (details: %s)", out_dir / "logs" / "run.log")
        get_logger().debug("traceback", exc_info=e.cause)
        _usage(ctx, log)
        return 1
    except ValueError as e:
        log.error("%s", e)
        return 2
    _usage(ctx, log)
    log.info("done in %.0fs%s", time.perf_counter() - t0, f" (cached: {', '.join(result.cached)})" if result.cached else "")
    if result.experience is None:
        return 0
    index = (out_dir / "index.html").resolve()
    log.info("open %s", index)
    log.info("for Q&A run: paper2podcast serve %s", out_dir)
    if args.serve:
        return _serve(out_dir, config, cache, port=args.port)
    return 0


def _usage(ctx, log) -> None:
    for client in ctx.clients():
        log.info("%s/%s: %s", client.provider.name, client.settings.model, client.usage.summary())


def _serve(out_dir: Path, config: Config, cache: Cache, port: int = 8765, host: str = "127.0.0.1",
           open_browser: bool = True, qa: bool = True) -> int:
    from .llm import LLMClient, LLMError, create_provider
    from .qa import QAEngine
    from .server import serve

    log = get_logger()
    engine = None
    if qa:
        try:
            engine = QAEngine(out_dir, LLMClient(create_provider(config.llm_for("qa")), cache))
        except (LLMError, FileNotFoundError) as e:
            log.warning("Q&A disabled: %s", e)
    serve(out_dir, engine, host=host, port=port, open_browser=open_browser)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    setup_logging(args.verbose)
    out_dir = Path(args.dir)
    overrides = _overrides(args)
    saved = out_dir / "p2p.config.json"
    if not args.config and saved.is_file():
        # reuse the configuration the experience was generated with
        base = json.loads(saved.read_text("utf-8"))
        from .config import _deep_merge

        config = Config.model_validate(_deep_merge(base, overrides))
    else:
        config, _ = load_config(args.config, overrides)
    cache = Cache(args.cache_dir or default_cache_dir())
    return _serve(out_dir, config, cache, port=args.port, host=args.host, open_browser=not args.no_browser, qa=not args.no_qa)


def cmd_init(args: argparse.Namespace) -> int:
    log = setup_logging()
    d = Path(args.dir)
    d.mkdir(parents=True, exist_ok=True)
    for name, content in (("paper2podcast.toml", EXAMPLE_CONFIG), ("profile.md", EXAMPLE_PROFILE)):
        target = d / name
        if target.exists() and not args.force:
            log.info("exists, kept: %s", target)
            continue
        target.write_text(content, "utf-8")
        log.info("wrote %s", target)
    log.info("next: edit profile.md, then run: paper2podcast paper.pdf --profile %s", d / "profile.md")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    from .llm import LLMError, create_provider
    from .tts import TTSError, create_tts

    log = setup_logging(args.verbose)
    config, found = load_config(args.config, _overrides(args))
    log.info("config: %s", found or "built-in defaults")
    ok = True
    try:
        provider = create_provider(config.llm)
        t0 = time.perf_counter()
        reply = provider.check()
        log.info("LLM %s/%s OK (%.1fs): %r", config.llm.provider, config.llm.model, time.perf_counter() - t0, reply)
    except Exception as e:  # report any provider failure as a failed check, not a traceback
        ok = False
        log.error("LLM %s/%s FAILED: %s", config.llm.provider, config.llm.model, e if isinstance(e, LLMError) else repr(e))
    tts_name = args.tts or config.tts.provider
    try:
        tts = create_tts(config.tts.model_copy(update={"provider": tts_name}))
        if tts is None:
            log.info("TTS %s: nothing to check (speech happens in the browser)", tts_name)
        else:
            audio = tts.synthesize("paper2podcast is ready.", tts.voice_for("narrator"))
            log.info("TTS %s OK (%d bytes)", tts_name, len(audio))
    except TTSError as e:
        ok = False
        log.error("TTS %s FAILED: %s", tts_name, e)
    return 0 if ok else 1


def cmd_stages(_: argparse.Namespace) -> int:
    from .pipeline import STAGES

    for s in STAGES:
        deps = ", ".join(s.deps) or "-"
        print(f"{s.name:<11} v{s.version}  deps: {deps:<45} {s.description}")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to a legacy codepage
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    argv = _normalize_argv(list(sys.argv[1:] if argv is None else argv))
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    handler = {"run": cmd_run, "serve": cmd_serve, "init": cmd_init, "check": cmd_check, "stages": cmd_stages}[args.command]
    try:
        return handler(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
