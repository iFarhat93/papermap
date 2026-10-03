"""Stage - quiz: an end-of-paper comprehension test.

Per section, the model writes multiple-choice questions grounded in the section
text and adapted to the listener. Options are shuffled, then a second,
independent pass answers every question from the text alone; questions whose
answer key it disagrees with are dropped (a wrong key is worse than a missing
question). Numbers in answers must exist in the paper. A final pass picks the
set that covers the most ground without asking the same thing twice, and every
kept question links back to the narration beat that covers it, so a missed
question can send the reader to the right place.
"""

from __future__ import annotations

import hashlib
import random
import re

from pydantic import Field, field_validator

from ..grounding import find_quote, number_in_text
from ..llm import LLMOutputError
from ..models import (
    OVERVIEW_ID, AudienceProfile, Explanations, Model, ParsedPaper, Quiz, QuizQuestion, Understanding, choose,
)
from .base import SYSTEM_PROMPT, RunContext, Stage
from .common import context_block, grounding_text, numbers_in, section_tables

KINDS = ("concept", "mechanism", "result", "comparison", "limitation")
LETTERS = "ABCD"
MAX_PER_SECTION = 4  # candidates per request
TOP_UP_ROUNDS = 2  # extra requests when too many questions fail the checks
DUPLICATE = 0.6  # question-token overlap above which two questions count as the same


def _index(v) -> int:
    """An option letter ("C", "c)", "(C)", "Option C", "C: ...") as a 0-based index; anything else is -1."""
    m = re.fullmatch(r"(?:OPTION\s+)?\(?([A-D])(?:[).:]\s*.*)?", str(v).strip().upper(), re.S) if v is not None else None
    return LETTERS.index(m.group(1)) if m else -1


class QuestionOut(Model):
    question: str
    correct: str
    distractors: list[str]
    explanation: str
    quote: str = ""
    kind: str = "concept"
    difficulty: str = "medium"

    @field_validator("correct", "question", "explanation", mode="before")
    @classmethod
    def _text(cls, v):
        return "" if v is None else str(v).strip()

    @field_validator("distractors", mode="before")
    @classmethod
    def _distractors(cls, v):
        if isinstance(v, list):
            return [re.sub(r"^[A-D][).]\s+", "", str(o)).strip() for o in v if o is not None]
        return v

    @property
    def options(self) -> list[str]:
        return [self.correct] + self.distractors[:3]


class QuizOut(Model):
    questions: list[QuestionOut] = Field(default_factory=list)


class SelectOut(Model):
    keep: list[str] = Field(default_factory=list)


class CheckOut(Model):
    answers: list[str] = Field(default_factory=list)

    @field_validator("answers", mode="before")
    @classmethod
    def _answers(cls, v):
        return ["" if x is None else str(x) for x in v] if isinstance(v, list) else v


QUIZ_PROMPT = """TASK: quiz

Write {n} multiple-choice questions that test whether the listener truly understood the section "{title}" of the paper above.

THE LISTENER
{profile}

Rules:
- Test understanding, not trivia: mechanisms, design choices and why they were made, what the results show, how the work differs from prior work, limitations. {depth}
- {focus}
- One option is correct according to the section text. The 3 distractors are plausible but clearly wrong according to the text (common misconceptions make good distractors). Never "all/none of the above".
- All four options similar in length, style and specificity, so the correct one does not stand out.
- Short: questions max 30 words, options max 18 words.
- "explanation": 1-2 sentences on why the correct answer is right, grounded in the text.
- "quote": one sentence copied exactly from the section text that supports the correct answer.
- Use only facts and numbers stated in the section text. Write math as plain text or simple LaTeX ($...$).
{avoid}
Return JSON:
{{"questions": [{{"question": "...", "correct": "the correct answer", "distractors": ["wrong 1", "wrong 2", "wrong 3"], "explanation": "...", "quote": "...", "kind": "{kinds}", "difficulty": "easy|medium|hard"}}]}}"""

CHECK_PROMPT = """TASK: quiz_check

Answer these multiple-choice questions using only the section text above. For each question give the letter of the option the text supports, or "none" when no option, or more than one option, is correct according to the text.

{questions}

Return JSON: {{"answers": ["<letter for question 1>", "<letter for question 2>", ...]}}"""

SELECT_PROMPT = """TASK: quiz_select

Candidate questions for an end-of-paper quiz, one per line as: id [section] question -> correct answer

{lines}

Pick exactly {k} of them that together best test whether the listener understood the whole paper:
- never two questions that test the same idea or fact, even when they are worded differently;
- spread over the sections and over the aspects of the paper: the problem, the method and its design choices, the evidence and results, comparisons with prior work, limitations;
- prefer questions that need understanding over recall.

Return JSON: {{"keep": ["<id>", ...]}}"""

FOCUS = {
    "overview": "This is the overview: test the paper's central idea, the problem it solves and its main result.",
    "section": "Ask about what this section itself adds: its specific details, design choices, evidence, comparisons or caveats. "
               "Do not re-test the paper's central idea in general terms; other questions cover it.",
}

DEPTH = {
    "light": "Keep it conceptual; avoid notation.",
    "balanced": "Mix intuition and mechanism.",
    "deep": "Favor technical depth: how the mechanism works, what equations and results mean.",
}

_STOP = set("""about above after again against also because been before being between both could does doing
during each from further have having here into itself just more most much only other over same should some
such than that their them then there these they this those through under until very were what when where
which while will with would your paper section authors""".split())


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{4,}", text.lower()) if w not in _STOP}


def _overlap(a: set[str], b: set[str]) -> float:
    return len(a & b) / (len(a | b) or 1)


def _same(a: QuestionOut, b: QuestionOut) -> bool:
    """Near-identical questions, or the same question about the same answer."""
    q = _overlap(_tokens(a.question), _tokens(b.question))
    return q >= DUPLICATE or (q >= DUPLICATE / 2 and _overlap(_tokens(a.correct), _tokens(b.correct)) >= DUPLICATE)


def _plan(sizes: dict[str, int], target: int) -> list[tuple[str, int]]:
    """Candidates to request per section: proportional to its length, at least one
    each, ~50% more than needed so questions that fail the checks are absorbed."""
    total = sum(sizes.values()) or 1
    want = max(target + target // 2, len(sizes))
    plan = {sid: max(1, min(MAX_PER_SECTION, round(want * size / total))) for sid, size in sizes.items()}
    while sum(plan.values()) < want and any(n < MAX_PER_SECTION for n in plan.values()):
        sid = max((s for s in plan if plan[s] < MAX_PER_SECTION), key=lambda s: sizes[s] / (plan[s] + 1))
        plan[sid] += 1
    return list(plan.items())


def _problems(q: QuestionOut, source: str) -> list[str]:
    p = []
    opts = q.options
    if not q.question or not q.correct or not q.explanation:
        p.append("question, correct answer and explanation are required")
    if len(q.distractors) < 3 or any(not o for o in opts):
        p.append("needs 3 non-empty distractors")
    elif len({o.lower() for o in opts}) != 4:
        p.append("the four options must be different")
    if re.search(r"\b(all|none) of the above\b", " ".join(opts), re.I):
        p.append('no "all/none of the above"')
    if not p:
        bad = [n for n in numbers_in(q.correct + " " + q.explanation) if not number_in_text(n, source)]
        if bad:
            p.append(f"numbers not in the paper: {', '.join(bad[:4])}")
    return p


def _seed(text: str) -> int:
    return int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)


def _shuffle(options: list[str], key: str) -> tuple[list[str], int]:
    """Deterministic shuffle (seeded by the question) of options whose first entry is correct."""
    order = list(range(len(options)))
    random.Random(_seed(key)).shuffle(order)
    return [options[i] for i in order], order.index(0)


def _best_beat(exp: Explanations, section_id: str, text: str) -> str | None:
    """The narration beat of the section that best covers the question (deep view first)."""
    want = _tokens(text)
    best, best_score = None, 0.0
    for view in ("deep", "high"):
        sv = exp.views[view].section(section_id) if view in exp.views else None
        for b in sv.beats if sv else []:
            score = _overlap(want, _tokens(f"{b.narration} {b.subtitle} {b.quote or ''}"))
            if score > best_score:
                best, best_score = b.id, score
    return best


def _run(ctx: RunContext, deps: dict) -> Quiz:
    log = STAGE.log()
    cfg = ctx.config.pipeline
    if not cfg.quiz:
        log.info("quiz disabled")
        return Quiz()
    paper: ParsedPaper = deps["parse"]
    und: Understanding = deps["understand"]
    profile: AudienceProfile = deps["profile"]
    exp: Explanations = deps["review"]
    llm = ctx.llm("quiz")
    target = max(10, cfg.quiz_questions)

    sources: dict[str, str] = {}
    for su in und.sections:
        text = grounding_text(paper, und, su.id, cfg.max_section_chars)
        sources[su.id] = text + "".join("\n\n" + t.as_text() for t in section_tables(paper, und, su.id))
    sizes = {s.id: len(sources[s.id]) for s in und.sections if s.id != OVERVIEW_ID and sources[s.id].strip()}
    stats = {"asked": 0, "invalid": 0, "disputed": 0}

    def ask(job) -> tuple[list[tuple[str, QuestionOut, list[str], int]], dict[str, int], list[str]]:
        sid, n, avoid = job
        st = {"asked": 0, "invalid": 0, "disputed": 0}
        asked: list[str] = []
        su, source = und.section(sid), sources[sid]
        ctx_block = context_block(und.overview.title, f"SECTION TEXT ({su.title})", source)
        avoid_block = ("\nAlready asked about this section, so write different questions:\n"
                       + "\n".join(f"- {a}" for a in avoid) + "\n") if avoid else ""
        prompt = QUIZ_PROMPT.format(n=n, title=su.title, profile=profile.brief(), depth=DEPTH.get(profile.depth, ""),
                                    focus=FOCUS["overview" if sid == OVERVIEW_ID else "section"],
                                    kinds=" | ".join(KINDS), avoid=avoid_block)
        try:
            out = llm.complete_json(
                prompt, QuizOut, system=SYSTEM_PROMPT, context=ctx_block, tag=f"quiz.{sid}",
                check=lambda o: [] if any(not _problems(q, source) for q in o.questions) else
                ["no usable question: " + "; ".join(sorted({p for q in o.questions for p in _problems(q, source)})[:3])
                 if o.questions else "no questions returned"],
            )
        except LLMOutputError as e:
            log.warning("%s: no questions (%s)", sid, "; ".join(e.problems[:2]))
            return [], st, asked
        st["asked"] = min(len(out.questions), n + 1)
        asked = [q.question for q in out.questions[: n + 1] if q.question]
        candidates = []
        for q in out.questions[: n + 1]:
            probs = _problems(q, source)
            if probs:
                st["invalid"] += 1
                log.debug("%s: dropped (%s): %s", sid, "; ".join(probs), q.question[:90])
                continue
            options, answer = _shuffle(q.options, f"{sid}|{q.question}")
            candidates.append((q, options, answer))
        if not candidates:
            return [], st, asked
        # independent answer-key check on the shuffled options: keep only agreements
        listing = "\n\n".join(
            f"{k}. {q.question}\n" + "\n".join(f"   {LETTERS[i]}: {o}" for i, o in enumerate(options))
            for k, (q, options, _) in enumerate(candidates, start=1)
        )
        try:
            check = llm.complete_json(
                CHECK_PROMPT.format(questions=listing), CheckOut, system=SYSTEM_PROMPT, context=ctx_block,
                tag=f"quiz.check.{sid}",
                check=lambda o: [] if len(o.answers) == len(candidates) else
                [f"give exactly {len(candidates)} answers, one per question"],
            )
        except LLMOutputError as e:
            log.warning("%s: answer check failed (%s); the section's questions are dropped", sid, "; ".join(e.problems[:2]))
            return [], st, asked
        kept = []
        for (q, options, answer), got in zip(candidates, check.answers):
            if _index(got) != answer:
                st["disputed"] += 1
                log.debug("%s: answer key disputed (key %s, check %r): %s", sid, LETTERS[answer], got, q.question[:90])
                continue
            kept.append((sid, q, options, answer))
        return kept, st, asked

    pool: list[tuple[str, QuestionOut, list[str], int]] = []
    written: dict[str, list[str]] = {}  # every question asked so far, per section

    def add(jobs, results) -> None:
        for (sid, _, _), (batch, st, qs) in zip(jobs, results):
            for k, v in st.items():
                stats[k] += v
            written.setdefault(sid, []).extend(qs)
            for item in batch:
                if not any(_same(item[1], p[1]) for p in pool):
                    pool.append(item)

    plan = ([(OVERVIEW_ID, 1)] if und.section(OVERVIEW_ID) else []) + _plan(sizes, target)
    jobs = [(sid, n, ()) for sid, n in plan]
    add(jobs, ctx.parallel(ask, jobs))
    for _ in range(TOP_UP_ROUNDS):
        if len(pool) >= target or not sizes:
            break
        # ask the longest sections for more, telling each what it was already asked
        need = target - len(pool)
        ranked = sorted(sizes, key=lambda s: -sizes[s])
        counts: dict[str, int] = {}
        for k in range(need + max(2, need // 2)):
            sid = ranked[k % len(ranked)]
            counts[sid] = min(MAX_PER_SECTION, counts.get(sid, 0) + 1)
        jobs = [(sid, n, tuple(written.get(sid, [])[-12:])) for sid, n in counts.items()]
        log.info("%d questions so far; asking %d sections for more", len(pool), len(jobs))
        add(jobs, ctx.parallel(ask, jobs))

    rank = {s.id: i for i, s in enumerate(und.sections)}

    def round_robin(items):
        """`target` questions taken across sections in reading order, so the whole paper is covered."""
        by_section: dict[str, list] = {}
        for item in items:
            by_section.setdefault(item[0], []).append(item)
        order = sorted(by_section, key=lambda s: rank.get(s, 0))
        picked, depth = [], 0
        while len(picked) < target and any(len(by_section[s]) > depth for s in order):
            for sid in order:
                if len(by_section[sid]) > depth and len(picked) < target:
                    picked.append(by_section[sid][depth])
            depth += 1
        return picked

    def select(items):
        """Sections are written independently and often re-test the paper's main idea in other
        words; one pass over all candidates keeps a set that covers the most ground."""
        ids = {f"c{i}": item for i, item in enumerate(items, start=1)}
        titles = {s.id: s.title for s in und.sections}
        lines = "\n".join(f"{cid} [{titles.get(it[0], it[0])}] {it[1].question} -> {it[1].correct}" for cid, it in ids.items())
        norm = lambda o: list(dict.fromkeys(c.strip().lower() for c in o.keep if c.strip().lower() in ids))
        try:
            out = llm.complete_json(
                SELECT_PROMPT.format(lines=lines, k=target), SelectOut, system=SYSTEM_PROMPT,
                context=context_block(und.overview.title, "PAPER OVERVIEW", sources.get(OVERVIEW_ID, "")), tag="quiz.select",
                check=lambda o: [] if len(norm(o)) == target else [f"keep exactly {target} different ids from the list"],
            )
        except LLMOutputError as e:
            log.warning("question selection failed (%s); keeping a round-robin across sections", "; ".join(e.problems[:2]))
            return None
        return [ids[c] for c in norm(out)]

    picked = pool if len(pool) <= target else (select(pool) or round_robin(pool))
    picked.sort(key=lambda item: rank.get(item[0], 0))

    # spread the correct answers evenly over A-D (moving an option does not change what it says)
    slots = [k % len(LETTERS) for k in range(len(picked))]
    random.Random(_seed("|".join(item[1].question for item in picked))).shuffle(slots)
    questions = []
    for k, ((sid, q, options, answer), slot) in enumerate(zip(picked, slots), start=1):
        others = [o for i, o in enumerate(options) if i != answer]
        options, answer = others[:slot] + [options[answer]] + others[slot:], slot
        quote = find_quote(q.quote, sources[sid]) if q.quote else None
        questions.append(QuizQuestion(
            id=f"q{k}", section_id=sid, question=q.question, options=options, answer=answer,
            explanation=q.explanation, quote=quote or "", kind=choose(q.kind, KINDS, "concept"),
            difficulty=choose(q.difficulty, ("easy", "medium", "hard"), "medium"),
            beat_id=_best_beat(exp, sid, f"{q.question} {q.correct} {q.explanation} {q.quote}"),
        ))
    log.info("%d questions across %d sections (%d written, %d malformed, %d failed the answer check, %d passed)",
             len(questions), len({q.section_id for q in questions}), stats["asked"], stats["invalid"], stats["disputed"], len(pool))
    if len(questions) < 10:
        log.warning("only %d questions passed the checks (the quiz aims for at least 10)", len(questions))
    return Quiz(questions=questions)


STAGE = Stage(
    name="quiz",
    version="1",
    deps=("parse", "understand", "profile", "review"),
    output=Quiz,
    run=_run,
    key_extra=lambda ctx: {"quiz": ctx.config.pipeline.quiz, "n": ctx.config.pipeline.quiz_questions,
                           "chars": ctx.config.pipeline.max_section_chars},
    description="end-of-paper comprehension quiz: grounded multiple-choice questions with verified answer keys",
)
