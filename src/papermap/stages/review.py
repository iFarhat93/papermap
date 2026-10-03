"""Stage 5 - review: a second pass that checks every section's narration
against the paper (factual errors, numbers attributed to the wrong thing,
unsupported claims, repetition) and applies safe fixes before diagrams and
audio are produced. Disabled with `pipeline.review = false`."""

from __future__ import annotations

import re

from pydantic import Field

from ..grounding import number_in_text
from ..llm import LLMOutputError
from ..models import AudienceProfile, Explanations, Model, ParsedPaper, ReviewIssue, Understanding
from .base import SYSTEM_PROMPT, RunContext, Stage
from .common import context_block, grounding_text, numbers_in, section_tables


class IssueOut(Model):
    beat: str
    problem: str
    fix: str = ""


class BeatFix(Model):
    id: str
    narration: str
    subtitle: str = ""


class ReviewOut(Model):
    issues: list[IssueOut] = Field(default_factory=list)
    beats: list[BeatFix] = Field(default_factory=list)


REVIEW_PROMPT = """TASK: review

You are the fact-checker of an explainer of the paper above. Check the narration of the section "{title}" ({view} view) against the section text, before it is published.

THE LISTENER
{profile}

WHAT EARLIER SECTIONS ALREADY COVERED
{earlier}

NARRATION TO CHECK
{beats}

Look for, and only for:
1. Statements the section text contradicts or does not support.
2. Numbers attached to the wrong method, dataset, metric, setting or baseline (check the tables and sentences they come from).
3. Points that repeat what an earlier section already said, without adding anything.
4. Misleading simplifications this listener would object to.

Do not rewrite beats that are correct, do not change style or length, and do not add new content. Keep beat ids.

Return JSON:
{{"issues": [{{"beat": "<beat id>", "problem": "what is wrong, citing the text", "fix": "how you fixed it"}}],
 "beats": [{{"id": "<beat id>", "narration": "corrected narration", "subtitle": "corrected subtitle"}}]}}
List in "beats" only the beats you corrected. If everything is correct, return {{"issues": [], "beats": []}}."""


def _run(ctx: RunContext, deps: dict) -> Explanations:
    log = STAGE.log()
    exp: Explanations = deps["explain"]
    if not ctx.config.pipeline.review:
        log.info("review disabled")
        return exp
    paper: ParsedPaper = deps["parse"]
    und: Understanding = deps["understand"]
    profile: AudienceProfile = deps["profile"]
    llm = ctx.llm("review")
    cfg = ctx.config.pipeline
    out = exp.model_copy(deep=True)

    jobs = [(view, pos, sv) for view, v in out.views.items() for pos, sv in enumerate(v.sections) if sv.beats]

    def review(job):
        view, pos, sv = job
        grounding = grounding_text(paper, und, sv.section_id, cfg.max_section_chars)
        tables = section_tables(paper, und, sv.section_id)
        table_text = "\n\n".join(t.as_text() for t in tables)
        source = grounding + ("\n\n" + table_text if table_text else "") + "\n" + und.overview.key_result
        earlier = "\n".join(f"- {s.title}: {s.summary}" for s in und.sections[:pos]) or "- (this is the opening)"
        beats = "\n".join(f'{b.id}: "{b.narration}" | subtitle: "{b.subtitle}"' for b in sv.beats)
        try:
            res = llm.complete_json(
                REVIEW_PROMPT.format(title=sv.title, view=view, profile=profile.brief(), earlier=earlier, beats=beats),
                ReviewOut,
                system=SYSTEM_PROMPT,
                context=context_block(und.overview.title, f"SECTION TEXT ({sv.title})", source),
                tag=f"review.{view}.{sv.section_id}",
            )
        except LLMOutputError as e:
            log.warning("%s/%s: review skipped (%s)", view, sv.section_id, "; ".join(e.problems[:2]))
            return [], set()
        by_id = {b.id: b for b in sv.beats}
        short = {b.id.rsplit(".", 1)[-1]: b for b in sv.beats}  # tolerate "b2"
        fixed: set[str] = set()
        for fx in res.beats:
            beat = by_id.get(fx.id) or short.get(fx.id)
            narration = re.sub(r"\s+", " ", fx.narration).strip()
            if not beat or not narration or narration == beat.narration:
                continue
            # a correction must not introduce numbers that are not in the paper
            if any(not number_in_text(n, source) for n in numbers_in(narration + " " + fx.subtitle)):
                log.debug("%s: correction rejected (ungrounded number)", beat.id)
                continue
            beat.narration = narration
            if fx.subtitle.strip():
                beat.subtitle = re.sub(r"\s+", " ", fx.subtitle).strip().rstrip(".")
            fixed.add(beat.id)
        issues = []
        for it in res.issues:
            beat = by_id.get(it.beat) or short.get(it.beat)
            bid = beat.id if beat else it.beat
            issues.append(ReviewIssue(view=view, section_id=sv.section_id, beat=bid, problem=it.problem.strip(),
                                      fix=it.fix.strip(), applied=bid in fixed))
        return issues, fixed

    results = ctx.parallel(review, jobs)
    all_issues = [i for batch, _ in results for i in batch]
    out.review_issues = all_issues
    # count corrected beats, not issues: the model sometimes files an issue under a neighbouring beat id
    corrected = sum(len(fixed) for _, fixed in results)
    log.info("%d issues found in %d sections, %d beats corrected", len(all_issues), len(jobs), corrected)
    for i in all_issues[:12]:
        log.debug("  %s %s: %s -> %s", i.beat, "fixed" if i.applied else "noted", i.problem[:120], i.fix[:80])
    return out


STAGE = Stage(
    name="review",
    version="1",
    deps=("parse", "understand", "profile", "explain"),
    output=Explanations,
    run=_run,
    key_extra=lambda ctx: {"review": ctx.config.pipeline.review, "chars": ctx.config.pipeline.max_section_chars},
    description="fact-check the narration against the paper and fix errors before diagrams and audio",
)
