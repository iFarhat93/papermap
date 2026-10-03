"""Stage 8 - render: assemble ``experience.json`` and a self-contained
``index.html`` (data, CSS and JS inlined; audio files next to it), plus the
retrieval index used by the Q&A server."""

from __future__ import annotations

import json
import re
import shutil
from importlib import resources
from pathlib import Path

from .. import __version__
from ..config import stable_hash
from ..models import (
    OVERVIEW_ID,
    AudienceProfile,
    Diagrams,
    Experience,
    Explanations,
    KnowledgeGraph,
    Narration,
    PaperMeta,
    ParsedPaper,
    QAInfo,
    SectionMeta,
    Understanding,
    View,
)
from .base import RunContext, Stage
from .common import grounding_text, split_text

QA_CHUNK_CHARS = 1100


def _web_asset(name: str) -> str:
    return resources.files("paper2podcast").joinpath("web", name).read_text(encoding="utf-8")


def build_html(experience: Experience) -> str:
    data = json.dumps(experience.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
    data = data.replace("</", "<\\/")  # never let the payload close the <script> tag
    title = experience.paper.title.replace("&", "&amp;").replace("<", "&lt;")
    parts = {
        "{{TITLE}}": title,
        "/*{{STYLE}}*/": _web_asset("app.css"),
        "/*{{SCRIPT}}*/": _web_asset("app.js"),
        "{{DATA}}": data,
    }
    # single pass over the template only: inserted content is never re-scanned
    pattern = re.compile("|".join(re.escape(k) for k in parts))
    return pattern.sub(lambda m: parts[m.group(0)], _web_asset("index.html"))


def build_qa_index(paper: ParsedPaper, und: Understanding, max_chars: int) -> dict:
    chunks = []
    for su in und.sections:
        text = grounding_text(paper, und, su.id, max_chars) if su.id != OVERVIEW_ID else (
            "\n\n".join(paper.section(r).text for r in su.raw_section_ids if paper.section(r)) or paper.first_page_text
        )
        for part in split_text(text, QA_CHUNK_CHARS):
            if part.strip():
                chunks.append({"id": f"c{len(chunks) + 1}", "section": su.id, "title": su.title, "text": part.strip()})
    notes = {
        su.id: {
            "title": su.title,
            "summary": su.summary,
            "key_points": su.key_points,
            "results": [r.model_dump() for r in su.results],
            "equations": [e.model_dump() for e in su.equations],
        }
        for su in und.sections
    }
    return {"chunks": chunks, "notes": notes}


def assemble(
    paper: ParsedPaper,
    und: Understanding,
    profile: AudienceProfile,
    exp: Explanations,
    diagrams: Diagrams,
    kg: KnowledgeGraph,
    narration: Narration,
    fingerprint: str,
) -> Experience:
    views: dict[str, View] = {}
    for name, view in exp.views.items():
        v = view.model_copy(deep=True)
        for sv in v.sections:
            sv.diagram = diagrams.diagrams.get(f"{name}:{sv.section_id}")
            for b in sv.beats:
                b.focus = diagrams.focus.get(b.id, []) if sv.diagram else []
                b.audio = narration.clips.get(b.id)
        views[name] = v
    ov = und.overview
    return Experience(
        generator=f"paper2podcast {__version__}",
        fingerprint=fingerprint,
        paper=PaperMeta(
            title=ov.title,
            authors=ov.authors,
            source=paper.source,
            n_pages=paper.n_pages,
            method_name=ov.method_name,
            one_line=ov.one_line,
            problem=ov.problem,
            contribution=ov.contribution,
            key_result=ov.key_result,
        ),
        profile=profile,
        sections=[SectionMeta(id=s.id, title=s.title, role=s.role, pages=s.pages, summary=s.summary) for s in und.sections],
        views=views,
        graph=kg,
        narration=narration.info,
        qa=QAInfo(suggested_questions=exp.suggested_questions),
    )


def _run(ctx: RunContext, deps: dict) -> Experience:
    log = STAGE.log()
    paper: ParsedPaper = deps["parse"]
    und: Understanding = deps["understand"]
    narration: Narration = deps["narrate"]
    fingerprint = stable_hash(ctx.stage_keys)[:16]
    exp = assemble(paper, und, deps["profile"], deps["explain"], deps["diagrams"], deps["graph"], narration, fingerprint)

    out = ctx.out_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / "experience.json").write_text(json.dumps(exp.model_dump(mode="json"), ensure_ascii=False, indent=1), "utf-8")
    qa_index = build_qa_index(paper, und, ctx.config.pipeline.max_section_chars)
    (out / "qa_index.json").write_text(json.dumps(qa_index, ensure_ascii=False), "utf-8")
    (out / "p2p.config.json").write_text(json.dumps(ctx.config.public_dump(), ensure_ascii=False, indent=1), "utf-8")
    (out / "index.html").write_text(build_html(exp), "utf-8")

    missing = 0
    for rel in narration.clips.values():
        src = ctx.cache.root / rel
        dst = out / rel
        if src.is_file():
            if not dst.is_file() or dst.stat().st_size != src.stat().st_size:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
        else:
            missing += 1
    if missing:
        log.warning("%d audio clips missing from the cache; re-run with --force narrate", missing)
    log.info("wrote %s", Path(out / "index.html"))
    return exp


STAGE = Stage(
    name="render",
    version="1",
    deps=("parse", "understand", "profile", "explain", "diagrams", "graph", "narrate"),
    output=Experience,
    run=_run,
    uses_llm=False,
    cacheable=False,
    description="assemble experience.json + self-contained index.html",
)
