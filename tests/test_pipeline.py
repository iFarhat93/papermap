"""End-to-end pipeline tests on a generated PDF with the deterministic mock provider."""

from __future__ import annotations

import json

import pytest

from papermap.cache import Cache
from papermap.cli import main
from papermap.config import load_config
from papermap.llm import LLMClient, create_provider
from papermap.pipeline import run_pipeline
from papermap.qa import QAEngine
from papermap.stages.base import RunContext
from papermap.stages.parse import parse_pdf


def _ctx(tmp_path, pdf, profile_text="Name: Grace"):
    config, _ = load_config(None, {"llm": {"provider": "mock", "model": "mock"}, "pipeline": {"concurrency": 2, "use_latex": False}})
    return RunContext(config=config, cache=Cache(tmp_path / "cache"), out_dir=tmp_path / "out", source=str(pdf), profile_text=profile_text)


def test_parser_finds_structure(sample_pdf):
    paper = parse_pdf(sample_pdf)
    headings = [s.heading for s in paper.sections]
    assert headings[0] == "Abstract"
    assert {"1 Introduction", "2 Method", "3 Experiments", "4 Conclusion"} <= set(headings)
    assert len(paper.references) == 5
    assert "Sparse Mixture Routing" in paper.title
    assert "41.2 accuracy" in paper.section("r1").text


@pytest.fixture(scope="module")
def generated(tmp_path_factory, sample_pdf):
    tmp = tmp_path_factory.mktemp("run")
    ctx = _ctx(tmp, sample_pdf)
    result = run_pipeline(ctx)
    return tmp, ctx, result


def test_pipeline_produces_the_artifact(generated):
    tmp, ctx, result = generated
    out = ctx.out_dir
    for name in ("index.html", "experience.json", "qa_index.json", "papermap.config.json"):
        assert (out / name).is_file(), name
    exp = json.loads((out / "experience.json").read_text("utf-8"))
    assert exp["paper"]["title"]
    assert exp["sections"][0]["id"] == "s0"
    assert set(exp["views"]) == {"high", "deep"}
    for view in exp["views"].values():
        assert [s["section_id"] for s in view["sections"]] == [s["id"] for s in exp["sections"]]
        assert all(s["beats"] for s in view["sections"])
    diagrams = [s["diagram"] for v in exp["views"].values() for s in v["sections"] if s["diagram"]]
    assert diagrams and {d["type"] for d in diagrams} >= {"flow"}
    assert exp["graph"]["center"] == "this-work"
    # source viewer: rendered pages, every beat points at a page, quotes are highlighted
    assert exp["paper"]["pages"] and (out / exp["paper"]["pages"][0]["src"]).is_file()
    beats = [b for v in exp["views"].values() for s in v["sections"] for b in s["beats"]]
    assert all(b["source"] and b["source"]["page"] >= 1 for b in beats)
    assert any(b["source"]["rects"] for b in beats if b["quote"])
    html = (out / "index.html").read_text("utf-8")
    assert "{{DATA}}" not in html and "/*{{SCRIPT}}*/" not in html
    assert '<script id="papermap-data" type="application/json">{' in html
    assert (out / "debug" / "understand.json").is_file()


def test_quotes_are_verbatim(generated):
    _, ctx, _ = generated
    exp = json.loads((ctx.out_dir / "experience.json").read_text("utf-8"))
    paper = json.loads((ctx.out_dir / "debug" / "parse.json").read_text("utf-8"))
    full = " ".join(s["text"] for s in paper["sections"]).replace("\n", " ")
    full = " ".join(full.split())
    quotes = [b["quote"] for v in exp["views"].values() for s in v["sections"] for b in s["beats"] if b["quote"]]
    assert quotes
    for q in quotes:
        assert " ".join(q.split()) in full


def test_rerun_is_cached_and_reproducible(generated, sample_pdf):
    tmp, ctx, first = generated
    before = (ctx.out_dir / "experience.json").read_text("utf-8")
    ctx2 = _ctx(tmp, sample_pdf)
    second = run_pipeline(ctx2)
    assert set(second.cached) == {"parse", "understand", "profile", "explain", "review", "diagrams", "graph", "narrate"}
    assert (ctx2.out_dir / "experience.json").read_text("utf-8") == before


def test_profile_change_invalidates_only_downstream(generated, sample_pdf):
    tmp, _, _ = generated
    ctx = _ctx(tmp, sample_pdf, profile_text="Name: Linus\nDepth: deep")
    result = run_pipeline(ctx)
    assert {"parse", "understand", "graph"} <= set(result.cached)
    assert "profile" not in result.cached and "explain" not in result.cached


def test_force_recomputes_a_stage(generated, sample_pdf):
    tmp, _, _ = generated
    result = run_pipeline(_ctx(tmp, sample_pdf), force=["graph"])
    assert "graph" not in result.cached and "understand" in result.cached


def test_qa_engine_answers_with_section_refs(generated):
    _, ctx, _ = generated
    engine = QAEngine(ctx.out_dir, LLMClient(create_provider(ctx.config.llm), ctx.cache))
    res = engine.answer("What accuracy does the method reach on GLUE?", {"view": "deep", "section_id": "s1"})
    assert res["answer"]
    assert res["refs"] and all(r.startswith("s") for r in res["refs"])


def test_cli_run_end_to_end(tmp_path, sample_pdf, profile_md, monkeypatch):
    monkeypatch.setenv("PAPERMAP_CACHE_DIR", str(tmp_path / "cache"))
    out = tmp_path / "site"
    code = main([str(sample_pdf), "--profile", str(profile_md), "--provider", "mock", "-o", str(out), "-q", "--no-latex"])
    assert code == 0
    assert (out / "index.html").is_file()
    (out / "index.html").unlink()
    assert main(["render", str(out)]) == 0  # rebuild the page from experience.json, no model calls
    assert "papermap-data" in (out / "index.html").read_text("utf-8")
    assert main(["stages"]) == 0


def test_cli_unknown_stage_is_an_error(tmp_path, sample_pdf, monkeypatch):
    monkeypatch.setenv("PAPERMAP_CACHE_DIR", str(tmp_path / "cache"))
    assert main([str(sample_pdf), "--provider", "mock", "-o", str(tmp_path / "o"), "--force", "nope", "-q", "--no-latex"]) == 2
