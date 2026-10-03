"""Stage 5 - diagrams: turn each diagram decision into a typed, renderable
diagram spec (flow / bar / table / equation) plus, for every beat, the diagram
elements to highlight while it plays. Numbers must come from the paper text.
"""

from __future__ import annotations

import re

from pydantic import Field, field_validator, model_validator

from ..grounding import number_in_text
from ..llm import LLMOutputError
from ..models import AudienceProfile, Diagram, Diagrams, Explanations, Model, ParsedPaper, Understanding
from .base import SYSTEM_PROMPT, RunContext, Stage
from .common import context_block, grounding_text


class DiagramOut(Model):
    diagram: dict
    focus: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _wrap(cls, data):
        # tolerate the diagram fields being returned at the top level
        if isinstance(data, dict) and "diagram" not in data and "type" in data:
            focus = data.pop("focus", {})
            return {"diagram": data, "focus": focus}
        return data

    @field_validator("focus", mode="before")
    @classmethod
    def _focus(cls, v):
        if not isinstance(v, dict):
            return {}
        return {str(k): ([str(x) for x in val] if isinstance(val, list) else [str(val)]) for k, val in v.items()}


TYPE_SPECS = {
    "flow": """"diagram": {{"type": "flow", "title": "max 6 words", "caption": "one-sentence takeaway", "direction": "LR or TB",
  "nodes": [{{"id": "short_snake_case_id", "label": "max 4 words", "detail": "one sentence shown on hover", "kind": "input | process | model | data | output | metric | concept | decision", "group": "a group id or null"}}],
  "edges": [{{"source": "node id", "target": "node id", "label": "max 3 words, may be empty"}}],
  "groups": [{{"id": "short_id", "label": "max 3 words"}}]}}
Rules: {min_nodes}-{max_nodes} nodes, every node connected by at least one edge. Labels name the actual components from the paper, not generic boxes. Use groups for blocks such as an encoder or a training loop (optional). "LR" for pipelines and data flow, "TB" for stacks and hierarchies.""",
    "bar": """"diagram": {{"type": "bar", "title": "max 6 words", "caption": "one-sentence takeaway", "categories": ["method or setting", "..."], "series": [{{"name": "metric or condition", "values": [1.0, 2.0]}}], "unit": "e.g. BLEU, %, ms", "higher_is_better": true}}
Rules: copy every value exactly as it appears in the section text (table cells count). 2-8 categories, 1-3 series, one value per category in each series (null if missing). Include the paper's method and its main baselines.""",
    "table": """"diagram": {{"type": "table", "title": "max 6 words", "caption": "one-sentence takeaway", "columns": ["", "column", "..."], "rows": [["row label", "cell", "..."]]}}
Rules: 2-5 columns, 2-7 rows, the first cell of each row is its label, cells max 6 words, numbers copied exactly from the text.""",
    "equation": """"diagram": {{"type": "equation", "title": "max 6 words", "caption": "one-sentence takeaway", "latex": "the equation in LaTeX, without $ delimiters", "terms": [{{"symbol": "LaTeX of one symbol", "meaning": "plain-language meaning"}}]}}
Rules: the paper's own equation, reconstructed faithfully (PDF extraction may scramble symbols); 3-7 terms.""",
}

FOCUS_KIND = {
    "flow": "node or group ids",
    "bar": "category names",
    "table": "row labels (first cell)",
    "equation": "term symbols",
}

DIAGRAM_PROMPT = """TASK: diagram

Design one {type} diagram for the {view_name} of the section "{title}" of the paper above.
What it must show: {brief}

Audience: {audience}. {view_hint}

Section facts:
{facts}

Narration beats that will play over this diagram:
{beats}

Return JSON:
{{{spec},
 "focus": {{"<beat id>": ["element", "..."]}}}}

"focus" maps every beat id above to the 1-3 diagram elements ({focus_kind}) to highlight while that beat plays, following the order in which the narration walks through the diagram."""

VIEW_HINT = {
    "high": "High-level view: intuitive and uncluttered; plain-language labels; at most {max} elements.",
    "deep": "Deep-dive view: precise and technical; the paper's own terms; at most {max} elements.",
}


def _facts(su) -> str:
    lines = [f"Summary: {su.summary}"] + [f"- {k}" for k in su.key_points]
    lines += [f"- result: {r.claim}" + (f" ({r.metric}: {r.value})" if r.value else "") for r in su.results]
    lines += [f"- equation: {e.latex} ({e.meaning})" for e in su.equations]
    return "\n".join(lines)


def _grounding_check(grounding: str):
    def check(out: DiagramOut) -> list[str]:
        try:
            d = Diagram.model_validate({"id": "tmp", "title": "", **out.diagram})
        except Exception as e:  # noqa: BLE001 - surfaced to the model as feedback
            return [f"diagram does not match the schema: {e}"[:400]]
        problems = d.problems()
        if d.type == "bar":
            for s in d.series:
                for cat, v in zip(d.categories, s.values):
                    if v is not None and not number_in_text(v, grounding):
                        problems.append(f"value {v} ({s.name} / {cat}) does not appear in the section text")
        if d.type == "table":
            for row in d.rows:
                for cell in row[1:]:
                    for n in re.findall(r"\d+(?:\.\d+)?", cell):
                        if len(n) >= 2 and not number_in_text(n, grounding):
                            problems.append(f"number {n} in row {row[0]!r} does not appear in the section text")
        return problems[:10]

    return check


def _run(ctx: RunContext, deps: dict) -> Diagrams:
    log = STAGE.log()
    paper: ParsedPaper = deps["parse"]
    und: Understanding = deps["understand"]
    profile: AudienceProfile = deps["profile"]
    exp: Explanations = deps["explain"]
    llm = ctx.llm("diagrams")
    cfg = ctx.config.pipeline

    jobs = [(view, sv) for view, v in exp.views.items() for sv in v.sections if sv.diagram_brief.needed and sv.beats]

    def make(job):
        view, sv = job
        su = und.section(sv.section_id)
        brief = sv.diagram_brief
        grounding = grounding_text(paper, und, sv.section_id, cfg.max_section_chars)
        max_el = 6 if view == "high" else 12
        spec = TYPE_SPECS[brief.type].format(min_nodes=3, max_nodes=max_el)
        beats = "\n".join(f'{b.id}: "{b.narration}"' for b in sv.beats)
        prompt = DIAGRAM_PROMPT.format(
            type=brief.type,
            view_name="high-level view" if view == "high" else "deep-dive view",
            title=sv.title,
            brief=brief.brief or f"the core idea of {sv.title}",
            audience=f"{profile.expertise_level} ({profile.summary})",
            view_hint=VIEW_HINT[view].format(max=max_el),
            facts=_facts(su),
            beats=beats,
            spec=spec,
            focus_kind=FOCUS_KIND[brief.type],
        )
        try:
            out = llm.complete_json(
                prompt,
                DiagramOut,
                system=SYSTEM_PROMPT,
                context=context_block(und.overview.title, f"SECTION TEXT ({sv.title})", grounding),
                check=_grounding_check(grounding),
                tag=f"diagrams.{view}.{sv.section_id}",
            )
        except LLMOutputError as e:
            log.warning("%s/%s: no diagram (%s)", view, sv.section_id, "; ".join(e.problems[:2]))
            return None
        d = Diagram.model_validate({"title": sv.title, **out.diagram, "id": f"{view}-{sv.section_id}"})
        if not d.title:
            d.title = sv.title
        valid = set(d.element_ids())
        focus = {}
        for b in sv.beats:
            ids = [x for x in out.focus.get(b.id, []) if x in valid]
            if not ids:  # tolerate "b1" instead of the full beat id
                short = b.id.rsplit(".", 1)[-1]
                ids = [x for x in out.focus.get(short, []) if x in valid]
            focus[b.id] = ids[:3]
        return f"{view}:{sv.section_id}", d, focus

    results = [r for r in ctx.parallel(make, jobs) if r]
    out = Diagrams()
    for key, d, focus in results:
        out.diagrams[key] = d
        out.focus.update(focus)
    by_type: dict[str, int] = {}
    for d in out.diagrams.values():
        by_type[d.type] = by_type.get(d.type, 0) + 1
    log.info("%d/%d diagrams built %s", len(out.diagrams), len(jobs), by_type)
    return out


STAGE = Stage(
    name="diagrams",
    version="1",
    deps=("parse", "understand", "profile", "explain"),
    output=Diagrams,
    run=_run,
    key_extra=lambda ctx: {"chars": ctx.config.pipeline.max_section_chars},
    description="typed diagram specs + per-beat highlight focus, numbers grounded in the text",
)
