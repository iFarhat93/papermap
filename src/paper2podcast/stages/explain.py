"""Stage 4 - explain: for every section and both views (high-level, deep-dive),
write narrated *beats* adapted to the listener, and decide whether a diagram
should carry the explanation."""

from __future__ import annotations

import re

from pydantic import Field

from ..grounding import find_quote, number_in_text
from ..models import (
    OVERVIEW_ID,
    AudienceProfile,
    Beat,
    DiagramBrief,
    Explanations,
    Model,
    ParsedPaper,
    SectionUnderstanding,
    SectionView,
    Understanding,
    View,
)
from .base import SYSTEM_PROMPT, RunContext, Stage
from .common import context_block, grounding_text, numbers_in


class BeatOut(Model):
    speaker: str = "narrator"
    narration: str
    subtitle: str
    quote: int | str | None = None


class ExplainOut(Model):
    beats: list[BeatOut]
    diagram: DiagramBrief = Field(default_factory=DiagramBrief)


class QuestionsOut(Model):
    questions: list[str]


VIEW_RULES = {
    "high": (
        "HIGH-LEVEL view - the intuition. Convey what this part is about and why it matters: the main idea, "
        "the motivation and the intuition behind it. Skip notation, hyperparameters and secondary details. "
        "Someone who only watches this view should still understand the paper's story."
    ),
    "deep": (
        "DEEP-DIVE view - the technical substance. Explain precisely how it works: the mechanism, the design "
        "choices and why they were made, what the equations mean, the experimental setup, results with their "
        "exact numbers, and limitations. Match the listener's depth preference ({depth}); stay concise - no filler."
    ),
}

SPEAKER_RULES = {
    "narrator": 'One narrator: set "speaker" to "narrator" for every beat.',
    "duo": (
        'Two voices in conversation: "host" (curious; voices the question this listener would ask; one short '
        'sentence) and "expert" (explains). Most beats are expert beats; use a host beat only where a question '
        "genuinely moves the explanation forward."
    ),
}

EXPLAIN_PROMPT = """TASK: explain

You are writing the narration for one section of an interactive, podcast-style explainer of the paper above.

THE LISTENER
{profile}

THE VIEW
{view_rule}

THE EPISODE (all sections, in order; each covers only its own part)
{outline}

THIS SECTION: "{title}" (section {pos} of {total}; role: {role})
Summary: {summary}
Key points:
{key_points}{results}{equations}{concepts}
Verified quotes from the paper (attach one to a beat by its number):
{quotes}

Previous section: {prev}. Next section: {next}.

WRITE {lo}-{hi} BEATS. A beat is one step of the explanation: what is said while one idea is on screen.
- "narration": 1-3 spoken sentences, max 55 words. Natural spoken language: no markdown, no citations, no "in this section". Explain the idea; don't announce it. Define a term only if this listener probably doesn't know it. Use an analogy from the listener's world only where it truly clarifies.
- "subtitle": the on-screen caption, max 12 words: the beat's key idea, not a copy of its first sentence.
- "quote": the number of the verified quote that supports this beat, or null. At most 2 quotes per section.
- "speaker": {speaker_rule}
{continuity} Use only numbers listed above. Vary your openings: never begin with "Imagine", "In this section" or "So,".

DIAGRAM DECISION
Would a diagram carry this section's explanation better than words? Prefer one for architectures, pipelines, processes, comparisons of results and central equations; skip it only when the content is purely narrative.
- "flow": architecture, process, data flow or relations between concepts
- "bar": comparing numeric results (only if 2+ comparable numbers are listed above)
- "table": structured comparison of options or settings{equation_rule}
"brief": one sentence saying exactly what the diagram should show.

Return JSON:
{{"beats": [{{"speaker": "...", "narration": "...", "subtitle": "...", "quote": null}}], "diagram": {{"needed": true, "type": "flow", "brief": "..."}}}}"""

QUESTIONS_PROMPT = """TASK: questions

The listener described below has just finished a guided explanation of the paper above.

THE LISTENER
{profile}

Write 5 questions this listener would most likely want to ask next. Make them specific to this paper, answerable from it, and varied: mechanism, results, limitations, relation to prior work, practical use.

Return JSON: {{"questions": ["...", "...", "...", "...", "..."]}}"""


def _bullets(items: list[str], prefix: str = "- ") -> str:
    return "\n".join(f"{prefix}{x}" for x in items) if items else "- (none)"


def _beat_range(su: SectionUnderstanding, view: str, cfg) -> tuple[int, int]:
    lo, hi = cfg.high_beats if view == "high" else cfg.deep_beats
    if su.id == OVERVIEW_ID:  # the opening stays short in both views
        lo, hi = (lo, max(lo, hi - 1)) if view == "high" else (max(2, lo - 1), max(3, hi - 2))
    return lo, hi


def _section_prompt(
    su: SectionUnderstanding,
    pos: int,
    sections: list[SectionUnderstanding],
    profile: AudienceProfile,
    view: str,
    ctx: RunContext,
) -> str:
    lo, hi = _beat_range(su, view, ctx.config.pipeline)
    results = ""
    if su.results:
        results = "\nResults (exact numbers):\n" + _bullets(
            [f"{r.claim}" + (f" [{r.metric}: {r.value}" + (f" vs {r.baseline}" if r.baseline else "") + "]" if r.value else "") for r in su.results]
        )
    equations = ""
    if su.equations and view == "deep":
        equations = "\nEquations:\n" + _bullets([f"{e.latex}  - {e.meaning}" for e in su.equations])
    concepts = ""
    if su.concepts:
        concepts = "\nConcepts:\n" + _bullets([f"{c.name}: {c.explanation}" for c in su.concepts])
    quotes = "\n".join(f'[{i}] "{q}"' for i, q in enumerate(su.key_quotes, start=1)) or "(none)"
    prev_title = f'"{sections[pos - 1].title}"' if pos > 0 else "none (this is the opening)"
    next_title = f'"{sections[pos + 1].title}"' if pos + 1 < len(sections) else "none (this is the final section)"
    style = ctx.config.pipeline.narration_style if ctx.config.pipeline.narration_style in SPEAKER_RULES else "narrator"
    outline = "\n".join(
        f"{i + 1}. {s.title}" + (" <- THIS SECTION" if s.id == su.id else "") + f": {s.summary.split('. ')[0].rstrip('.')}."
        for i, s in enumerate(sections)
    )
    if su.id == OVERVIEW_ID:
        continuity = (
            "This is the opening: hook the listener with the problem, then give the core idea and why it matters. "
            "Do not walk through the later sections."
        )
    else:
        continuity = (
            "The listener has already heard the opening and the earlier sections: do not re-introduce the paper or "
            "its method. The first beat flows from the previous section without recapping it."
        )
    return EXPLAIN_PROMPT.format(
        outline=outline,
        continuity=continuity,
        profile=profile.brief(),
        view_rule=VIEW_RULES[view].format(depth=profile.depth),
        title=su.title,
        pos=pos + 1,
        total=len(sections),
        role=su.role,
        summary=su.summary,
        key_points=_bullets(su.key_points),
        results=results,
        equations=equations,
        concepts=concepts,
        quotes=quotes,
        prev=prev_title,
        next=next_title,
        lo=lo,
        hi=hi,
        speaker_rule=SPEAKER_RULES[style],
        equation_rule=(
            '\n- "equation": one central equation worth unpacking term by term' if view == "deep" and su.equations else ""
        ),
    )


def _beat_check(grounding: str, lo: int, hi: int):
    def check(out: ExplainOut) -> list[str]:
        if len(out.beats) < lo:
            return [f"only {len(out.beats)} beat(s) returned; write {lo}-{hi} beats"]
        bad = []
        for b in out.beats:
            for n in numbers_in(b.narration + " " + b.subtitle):
                if not number_in_text(n, grounding) and n not in bad:
                    bad.append(n)
        if bad:
            return [f"these numbers do not appear in the paper text and must be removed or corrected: {', '.join(bad[:8])}"]
        return []

    return check


MAX_QUOTES_PER_SECTION = 2


def _to_beats(out: ExplainOut, view: str, su: SectionUnderstanding, hi: int, style: str, grounding: str) -> list[Beat]:
    beats = []
    allowed = {"narrator"} if style == "narrator" else {"host", "expert"}
    used_quotes: list[str] = []
    for i, b in enumerate(out.beats[: hi + 1], start=1):
        quote = None
        if isinstance(b.quote, int) and 1 <= b.quote <= len(su.key_quotes):
            quote = su.key_quotes[b.quote - 1]
        elif isinstance(b.quote, str) and b.quote.strip():
            if b.quote.strip().isdigit() and 1 <= int(b.quote) <= len(su.key_quotes):
                quote = su.key_quotes[int(b.quote) - 1]
            else:
                quote = find_quote(b.quote, grounding)
        # quotes are emphasis: keep them rare, distinct and never on back-to-back beats
        if quote and (quote in used_quotes or len(used_quotes) >= MAX_QUOTES_PER_SECTION or (beats and beats[-1].quote)):
            quote = None
        if quote:
            used_quotes.append(quote)
        speaker = b.speaker.strip().lower() if b.speaker else ""
        if speaker not in allowed:
            speaker = "narrator" if style == "narrator" else "expert"
        beats.append(
            Beat(
                id=f"{view}.{su.id}.b{i}",
                speaker=speaker,
                narration=re.sub(r"\s+", " ", b.narration).strip(),
                subtitle=re.sub(r"\s+", " ", b.subtitle).strip().rstrip("."),
                quote=quote,
                refs=[su.id],
            )
        )
    return beats


def _run(ctx: RunContext, deps: dict) -> Explanations:
    log = STAGE.log()
    paper: ParsedPaper = deps["parse"]
    und: Understanding = deps["understand"]
    profile: AudienceProfile = deps["profile"]
    llm = ctx.llm("explain")
    cfg = ctx.config.pipeline
    style = cfg.narration_style if cfg.narration_style in SPEAKER_RULES else "narrator"
    sections = und.sections

    jobs = [(view, pos, su) for view in ("high", "deep") for pos, su in enumerate(sections)]

    def explain(job) -> SectionView:
        view, pos, su = job
        grounding = grounding_text(paper, und, su.id, cfg.max_section_chars)
        out = llm.complete_json(
            _section_prompt(su, pos, sections, profile, view, ctx),
            ExplainOut,
            system=SYSTEM_PROMPT,
            context=context_block(und.overview.title, f"SECTION TEXT ({su.title})", grounding),
            check=_beat_check(grounding + "\n" + und.overview.key_result, *_beat_range(su, view, cfg)),
            tag=f"explain.{view}.{su.id}",
        )
        beats = _to_beats(out, view, su, _beat_range(su, view, cfg)[1], style, grounding)
        brief = out.diagram
        if brief.type == "equation" and (view != "deep" or not su.equations):
            brief = brief.model_copy(update={"type": "flow"})
        return SectionView(section_id=su.id, title=su.title, beats=beats, diagram_brief=brief)

    results = ctx.parallel(explain, jobs)
    views = {
        v: View(kind=v, sections=[sv for (vv, _, _), sv in zip(jobs, results) if vv == v]) for v in ("high", "deep")
    }
    for v, view in views.items():
        n_beats = sum(len(s.beats) for s in view.sections)
        n_diag = sum(1 for s in view.sections if s.diagram_brief.needed)
        words = sum(len(b.narration.split()) for s in view.sections for b in s.beats)
        log.info("%s view: %d beats (~%.1f min), %d diagrams requested", v, n_beats, words / 150, n_diag)

    summary = "\n".join(f"- {s.title}: {s.summary}" for s in sections)
    try:
        qs = llm.complete_json(
            QUESTIONS_PROMPT.format(profile=profile.brief()),
            QuestionsOut,
            system=SYSTEM_PROMPT,
            context=context_block(und.overview.title, "PAPER OVERVIEW", f"{und.overview.one_line}\n\n{summary}"),
            tag="explain.questions",
        ).questions[:6]
    except Exception as e:  # suggestions are a nicety; never fail the run for them
        log.warning("could not generate suggested questions: %s", e)
        qs = []
    return Explanations(views=views, suggested_questions=[q.strip() for q in qs if q.strip()])


def _key(ctx: RunContext):
    p = ctx.config.pipeline
    return {"style": p.narration_style, "high": p.high_beats, "deep": p.deep_beats, "chars": p.max_section_chars}


STAGE = Stage(
    name="explain",
    version="1",
    deps=("parse", "understand", "profile"),
    output=Explanations,
    run=_run,
    key_extra=_key,
    description="profile-adapted narrated beats for high-level and deep-dive views; diagram decisions",
)
