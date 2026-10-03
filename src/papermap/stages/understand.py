"""Stage 2 - understand: chapter the paper into logical sections and extract,
per section, the summary, key points, verified quotes, equations, results,
concepts and prior work. Independent of the audience (reused across profiles).
"""

from __future__ import annotations

import re

from pydantic import Field

from ..llm import LLMOutputError
from ..grounding import find_quote, lower_text, name_in_text, number_in_text
from ..models import (
    OVERVIEW_ID,
    SECTION_ROLES,
    Concept,
    Equation,
    Model,
    PaperOverview,
    ParsedPaper,
    PriorWork,
    ResultItem,
    SectionUnderstanding,
    Understanding,
    choose,
)
from .base import SYSTEM_PROMPT, RunContext, Stage
from .common import context_block, raw_text, split_text

# ------------------------------------------------------------------ schemas


class PlanSection(Model):
    title: str
    role: str = "other"
    raw_section_ids: list[str]


class PlanOut(Model):
    title: str
    authors: list[str] = Field(default_factory=list)
    method_name: str = ""
    one_line: str
    problem: str
    contribution: str
    key_result: str = ""
    sections: list[PlanSection]


class AnalyzeOut(Model):
    summary: str
    key_points: list[str] = Field(default_factory=list)
    key_quotes: list[str] = Field(default_factory=list)
    equations: list[Equation] = Field(default_factory=list)
    results: list[ResultItem] = Field(default_factory=list)
    concepts: list[Concept] = Field(default_factory=list)
    prior_work: list[PriorWork] = Field(default_factory=list)


# ------------------------------------------------------------------ prompts

PLAN_PROMPT = """TASK: plan

You are planning a guided, section-by-section explanation of this paper.

Raw sections extracted from the PDF (id | heading | pages | length | opening words):
{outline}

1) Group the raw sections into {min_s}-{max_s} logical sections, in reading order, the way a great lecturer would chapter this paper.
- Merge small or closely related raw sections (for example all subsections of the method) into one logical section.
- Each raw section id may appear in at most one logical section. Keep the original order.
- Leave out the abstract (an overview covers it), references and acknowledgements{appendix_rule}.
- Give each logical section a short, informative title (max 6 words, normal capitalization) and a role from: {roles}.
- Aim to keep each logical section under about {max_chars} characters of raw text.

2) From the first page and abstract, extract:
- title: the exact paper title in normal capitalization
- authors: author names as printed (max 12)
- method_name: the short name of the proposed method/model/system, or "" if there is none
- one_line: one sentence (max 30 words): what the paper does and why it matters
- problem: 1-2 sentences on the problem addressed
- contribution: 1-2 sentences on the main contribution
- key_result: the headline result in one sentence with the exact number(s) from the abstract, or ""

Return JSON:
{{"title": "...", "authors": ["..."], "method_name": "...", "one_line": "...", "problem": "...", "contribution": "...", "key_result": "...", "sections": [{{"title": "...", "role": "method", "raw_section_ids": ["r3", "r4"]}}]}}"""

ANALYZE_PROMPT = """TASK: analyze

Analyze the section "{title}" (role: {role}) of the paper above, whose contribution in one line is: {one_line}

Return JSON with:
- "summary": 2-3 sentences capturing what this section establishes.
- "key_points": 3-6 specific statements (one sentence each), the ideas a reader must take away. Name the concrete mechanism, design choice or finding; no generic statements.
- "key_quotes": 1-3 sentences copied EXACTLY, character for character, from the section text, that best state its key ideas. Copy, never paraphrase.
- "equations": the central equations of this section (max 3), each {{"latex": "...", "meaning": "one plain sentence"}}. Reconstruct the LaTeX carefully: PDF extraction may scramble symbols, subscripts and fractions. Empty list if there are none.
- "results": quantitative findings stated in the text (max 6), each {{"claim": "one sentence", "metric": "...", "value": "the number exactly as written", "baseline": "what it is compared with, or empty"}}. Empty list if none.
- "concepts": technical terms this section relies on or introduces (max 6), each {{"name": "...", "explanation": "one sentence grounded in the text"}}.
- "prior_work": named prior methods, models, datasets or papers mentioned (max 8), each {{"name": "...", "relation": "how this paper relates to it, in a few words"}}.

Return only the JSON object."""


# ------------------------------------------------------------------ logic


def _outline(paper: ParsedPaper) -> str:
    rows = []
    for s in paper.sections:
        opening = re.sub(r"\s+", " ", s.text[:110]).strip()
        indent = "  " * (s.level - 1)
        rows.append(f'{s.id} | {indent}{s.heading} | p{s.page_start}-{s.page_end} | {len(s.text)} chars | "{opening}"')
    return "\n".join(rows)


def _abstract_id(paper: ParsedPaper) -> str | None:
    for s in paper.sections[:3]:
        if re.sub(r"[^a-z]", "", s.heading.lower()) == "abstract":
            return s.id
    return None


def _plan_check(paper: ParsedPaper, cfg):
    known = {s.id for s in paper.sections}

    def check(plan: PlanOut) -> list[str]:
        problems = []
        seen: set[str] = set()
        for sec in plan.sections:
            unknown = [r for r in sec.raw_section_ids if r not in known]
            if unknown:
                problems.append(f"section {sec.title!r} uses unknown raw ids {unknown}")
            dup = [r for r in sec.raw_section_ids if r in seen]
            if dup:
                problems.append(f"raw ids used twice: {dup}")
            seen.update(sec.raw_section_ids)
        valid = [s for s in plan.sections if any(r in known for r in s.raw_section_ids)]
        if len(valid) < max(1, cfg.min_sections - 1):
            problems.append(f"only {len(valid)} sections; need {cfg.min_sections}-{cfg.max_sections}")
        if len(valid) > cfg.max_sections + 2:
            problems.append(f"{len(valid)} sections is too many; need {cfg.min_sections}-{cfg.max_sections}")
        return problems

    return check


def _normalize_plan(paper: ParsedPaper, plan: PlanOut, max_chars: int) -> list[tuple[str, str, list[str]]]:
    """Clean ids (unknown/duplicate dropped), keep reading order, split oversize sections."""
    order = {s.id: i for i, s in enumerate(paper.sections)}
    used: set[str] = set()
    cleaned = []
    for sec in plan.sections:
        ids = [r for r in sec.raw_section_ids if r in order and r not in used]
        used.update(ids)
        if ids:
            cleaned.append((sec.title.strip() or "Section", choose(sec.role, SECTION_ROLES, "other"), sorted(ids, key=order.get)))
    cleaned.sort(key=lambda t: order[t[2][0]])

    out = []
    for title, role, ids in cleaned:
        units: list[tuple[str, int]] = []  # (raw id or part id, chars)
        for rid in ids:
            text = paper.section(rid).text
            if len(text) > max_chars:
                parts = split_text(text, max_chars)
                units += [(f"{rid}#{k + 1}", len(p)) for k, p in enumerate(parts)]
            else:
                units.append((rid, len(text)))
        groups: list[list[str]] = [[]]
        size = 0
        for uid, n in units:
            if groups[-1] and size + n > max_chars:
                groups.append([])
                size = 0
            groups[-1].append(uid)
            size += n
        groups = [g for g in groups if g]
        for k, g in enumerate(groups):
            t = title if len(groups) == 1 else f"{title} ({k + 1}/{len(groups)})"
            out.append((t, role, g))
    return out


def _fallback_plan(paper: ParsedPaper) -> list[PlanSection]:
    """One logical section per top-level raw section (used if the model's plan is unusable)."""
    secs: list[PlanSection] = []
    abs_id = _abstract_id(paper)
    for s in paper.sections:
        if s.id == abs_id:
            continue
        if s.level == 1 or not secs:
            secs.append(PlanSection(title=re.sub(r"^[\dIVXA-H.]+\s+", "", s.heading).title()[:60], raw_section_ids=[s.id]))
        else:
            secs[-1].raw_section_ids.append(s.id)
    return secs


def _clean_analysis(out: AnalyzeOut, text: str) -> dict:
    low = lower_text(text)
    quotes = []
    for q in out.key_quotes:
        found = find_quote(q, text)
        if found and found not in quotes:
            quotes.append(found)
    results = []
    for r in out.results[:6]:
        if r.value and re.search(r"\d", r.value) and not number_in_text(re.sub(r"[^\d.\-]", "", r.value) or r.value, text):
            r = r.model_copy(update={"value": ""})
        results.append(r)
    prior = [p for p in out.prior_work[:8] if name_in_text(p.name, low)]
    concepts = [c for c in out.concepts[:6] if name_in_text(c.name, low)] or out.concepts[:3]
    return {
        "summary": out.summary.strip(),
        "key_points": [k.strip() for k in out.key_points[:6] if k.strip()],
        "key_quotes": quotes[:3],
        "equations": out.equations[:3],
        "results": results,
        "concepts": concepts,
        "prior_work": prior,
    }


def _run(ctx: RunContext, deps: dict) -> Understanding:
    log = STAGE.log()
    paper: ParsedPaper = deps["parse"]
    cfg = ctx.config.pipeline
    llm = ctx.llm("understand")

    abs_id = _abstract_id(paper)
    abstract = paper.section(abs_id).text if abs_id else ""
    ctx_text = paper.first_page_text + (f"\n\nABSTRACT:\n{abstract}" if abstract and abstract[:200] not in paper.first_page_text else "")
    appendix_rule = "" if cfg.include_appendix else ", and appendices unless they are essential to understanding the main contribution"
    prompt = PLAN_PROMPT.format(
        outline=_outline(paper),
        min_s=cfg.min_sections,
        max_s=cfg.max_sections,
        appendix_rule=appendix_rule,
        roles=", ".join(r for r in SECTION_ROLES if r != "overview"),
        max_chars=cfg.max_section_chars,
    )
    try:
        plan = llm.complete_json(
            prompt, PlanOut, system=SYSTEM_PROMPT,
            context=context_block(paper.title, "FIRST PAGE", ctx_text),
            check=_plan_check(paper, cfg), tag="understand.plan",
        )
    except LLMOutputError as e:  # keep going with a structural plan rather than failing the run
        log.warning("plan output unusable (%s); falling back to the PDF's own sections", e)
        plan = PlanOut(title=paper.title, one_line="", problem="", contribution="", sections=_fallback_plan(paper))

    if abs_id:  # the abstract grounds the overview (s0); keep it out of the chapters
        for sec in plan.sections:
            if len(sec.raw_section_ids) > 1 and abs_id in sec.raw_section_ids:
                sec.raw_section_ids = [r for r in sec.raw_section_ids if r != abs_id]
    planned = _normalize_plan(paper, plan, cfg.max_section_chars)
    log.info("planned %d logical sections: %s", len(planned), " | ".join(t for t, _, _ in planned))

    overview = PaperOverview(
        title=plan.title.strip() or paper.title,
        authors=[a.strip() for a in plan.authors if a.strip()][:12],
        method_name=plan.method_name.strip(),
        one_line=plan.one_line.strip(),
        problem=plan.problem.strip(),
        contribution=plan.contribution.strip(),
        key_result=plan.key_result.strip(),
    )
    if overview.key_result and not all(number_in_text(n, paper.full_text) for n in re.findall(r"\d+(?:\.\d+)?", overview.key_result)):
        log.warning("key result %r contains numbers not found in the paper; dropping it", overview.key_result)
        overview.key_result = ""

    # The overview section (s0) is grounded on the abstract (or first page).
    jobs: list[tuple[str, str, str, list[str], str]] = [
        (OVERVIEW_ID, "The big picture", "overview", [abs_id] if abs_id else [], abstract or paper.first_page_text)
    ]
    for i, (title, role, ids) in enumerate(planned, start=1):
        text = "\n\n".join(raw_text(paper, rid, cfg.max_section_chars) for rid in ids)
        jobs.append((f"s{i}", title, role, ids, text))

    def analyze(job):
        sid, title, role, ids, text = job
        out = llm.complete_json(
            ANALYZE_PROMPT.format(title=title, role=role, one_line=overview.one_line or overview.title),
            AnalyzeOut,
            system=SYSTEM_PROMPT,
            context=context_block(overview.title, f"SECTION TEXT ({title})", text),
            tag=f"understand.analyze.{sid}",
        )
        pages: set[int] = set()
        for rid in ids:
            sec = paper.section(rid.partition("#")[0])
            if sec:
                pages.update(range(sec.page_start, sec.page_end + 1))
        cleaned = _clean_analysis(out, text)
        dropped = len(out.key_quotes) - len(cleaned["key_quotes"])
        if dropped:
            log.debug("%s: dropped %d unverifiable quote(s)", sid, dropped)
        return SectionUnderstanding(id=sid, title=title, role=role, raw_section_ids=ids, pages=sorted(pages), **cleaned)

    sections = ctx.parallel(analyze, jobs)
    s0 = sections[0]
    s0.key_points = [p for p in [overview.one_line, overview.contribution, overview.key_result] if p] + s0.key_points[:3]
    n_quotes = sum(len(s.key_quotes) for s in sections)
    log.info("analyzed %d sections (%d verified quotes)", len(sections), n_quotes)
    return Understanding(overview=overview, sections=sections)


def _key(ctx: RunContext):
    p = ctx.config.pipeline
    return {"min": p.min_sections, "max": p.max_sections, "chars": p.max_section_chars, "appendix": p.include_appendix}


STAGE = Stage(
    name="understand",
    version="1",
    deps=("parse",),
    output=Understanding,
    run=_run,
    key_extra=_key,
    description="chapter the paper; per-section summary, key points, verified quotes, equations, results",
)
