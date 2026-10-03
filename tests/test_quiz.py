"""Quiz stage: answer-key verification, filtering, coverage and the at-least-10 rule."""

from __future__ import annotations

import re
from collections import Counter

import pytest

from papermap.cache import Cache
from papermap.config import load_config
from papermap.llm import LLMOutputError
from papermap.models import (
    AudienceProfile, Beat, Explanations, PaperOverview, ParsedPaper, RawSection, SectionUnderstanding, SectionView,
    Understanding, View,
)
from papermap.stages import quiz
from papermap.stages.base import RunContext

TOPICS = {
    "s0": {"routing": "sends each token to a small subset of experts"},
    "s1": {"router": "picks one expert for every token", "capacity": "caps how many tokens an expert accepts",
           "gating": "scales each expert output by a learned weight", "overflow": "drops tokens that exceed the cap",
           "experts": "are small feed-forward networks", "dispatch": "groups tokens by their chosen expert"},
    "s2": {"temperature": "softens the router distribution early on", "schedule": "anneals the temperature over training",
           "balancing": "emerges without extra loss terms", "logits": "are divided by the temperature",
           "auxiliary": "loss is not needed for load balance", "variance": "of expert load shrinks during training"},
    "s3": {"throughput": "improves on large accelerator pools", "interconnect": "links the accelerators at high bandwidth",
           "baseline": "is a dense transformer of equal compute", "benchmark": "suite includes GLUE and SuperGLUE",
           "accuracy": "rises over the dense baseline", "scaling": "continues as more experts are added"},
}


def _paper_and_views():
    sections, und_sections, beats = [], [], {}
    for k, (sid, facts) in enumerate(TOPICS.items()):
        text = " ".join(f"The {w} {fact}." for w, fact in facts.items()) + " Accuracy reaches 41.2 points."
        sections.append(RawSection(id=f"r{k}", heading="Abstract" if k == 0 else f"{k} Part", text=text,
                                   page_start=1, page_end=1))
        und_sections.append(SectionUnderstanding(id=sid, title=f"Part {k}", raw_section_ids=[f"r{k}"], summary="A part."))
        beats[sid] = [Beat(id=f"deep.{sid}.b{i + 1}", narration=f"Here is why the {w} matters: it {fact}.", subtitle=w)
                      for i, (w, fact) in enumerate(facts.items())]
    paper = ParsedPaper(source="x.pdf", sha256="0", title="Routing", first_page_text="Routing", n_pages=1, sections=sections)
    und = Understanding(overview=PaperOverview(title="Routing", one_line="", problem="", contribution=""),
                        sections=und_sections)
    view = View(kind="deep", sections=[SectionView(section_id=s, title=s, beats=b) for s, b in beats.items()])
    return paper, und, Explanations(views={"deep": view})


class FakeLLM:
    """Writes one question per topic of the section (skipping topics it was already asked
    about). The checker answers from the 'text', except for questions marked DISPUTE.

    ``mode`` shapes the first round: "mixed" adds malformed, ungrounded and disputed
    questions; "mostly_disputed" fails two sections' first round so top-ups are needed;
    "clean" keeps everything. The final selection keeps the last candidates listed (so
    tests can tell it was used), or returns too few with ``select="bad"``."""

    def __init__(self, mode: str = "mixed", select: str = "last"):
        self.mode = mode
        self.select = select
        self.prompts: list[str] = []

    def complete_json(self, prompt, schema, *, system=None, context=None, check=None, tag="", **kw):
        self.prompts.append(prompt)
        if schema is quiz.QuizOut:
            data = self._quiz(prompt, tag)
        elif schema is quiz.SelectOut:
            data = self._select(prompt)
        else:
            data = self._check(prompt)
        obj = schema.model_validate(data)
        if check and check(obj):  # what LLMClient does once its retries are exhausted
            raise LLMOutputError(tag, check(obj))
        return obj

    def _quiz(self, prompt: str, tag: str):
        sid = tag.split(".")[1]
        n = int(re.search(r"Write (\d+) multiple-choice", prompt).group(1))
        top_up = "Already asked" in prompt
        asked = prompt.split("Already asked", 1)[1] if top_up else ""
        facts = [(w, f) for w, f in TOPICS[sid].items() if f" {w} " not in asked]
        out = []
        for w, fact in facts[:n]:
            q = {"question": f"What is true of the {w} in this method?", "correct": f"It {fact}",
                 "distractors": ["It is removed after pretraining", "It doubles the inference cost", "It is never evaluated"],
                 "explanation": f"The paper says the {w} {fact}.", "quote": f"The {w} {fact}.", "kind": "mechanism"}
            if self.mode == "mostly_disputed" and not top_up and sid in ("s1", "s2"):
                q["question"] += " DISPUTE"
            out.append(q)
        if self.mode == "mixed" and not top_up and sid == "s1":
            out[0]["question"] += " DISPUTE"
            out[1]["distractors"] = out[1]["distractors"][:2]  # malformed
            out[2]["correct"] = "It reaches 97.3 accuracy"  # number not in the paper
        return {"questions": out}

    def _check(self, prompt: str):
        answers = []
        for block in re.findall(r"^\d+\. (.+?)(?=^\d+\. |\Z)", prompt, re.M | re.S):
            options = re.findall(r"^\s+([A-D]): (.+)$", block, re.M)
            facts = {f"It {f}" for t in TOPICS.values() for f in t.values()}
            right = next((letter for letter, text in options if text in facts), "none")
            answers.append(("A" if right != "A" else "B") if "DISPUTE" in block else right)
        return {"answers": answers}

    def _select(self, prompt: str):
        k = int(re.search(r"Pick exactly (\d+)", prompt).group(1))
        ids = re.findall(r"^(c\d+) \[", prompt, re.M)
        return {"keep": ids[: k - 1] if self.select == "bad" else ids[-k:]}


def _ctx(tmp_path, llm, **pipeline):
    config, _ = load_config(None, {"llm": {"provider": "mock", "model": "mock"},
                                   "pipeline": {"concurrency": 1, **pipeline}})
    ctx = RunContext(config=config, cache=Cache(tmp_path / "cache"), out_dir=tmp_path, source="x.pdf", profile_text="")
    ctx._clients["quiz"] = llm
    return ctx


def _run(ctx):
    paper, und, exp = _paper_and_views()
    return quiz.STAGE.run(ctx, {"parse": paper, "understand": und, "profile": AudienceProfile(), "review": exp})


def test_quiz_keeps_only_verified_well_formed_questions(tmp_path):
    result = _run(_ctx(tmp_path, FakeLLM("mixed")))
    qs = result.questions
    assert len(qs) >= 10
    assert all("DISPUTE" not in q.question for q in qs)
    assert all(len(q.options) == 4 and len(set(q.options)) == 4 for q in qs)
    facts = {f"It {f}" for t in TOPICS.values() for f in t.values()}
    assert all(q.options[q.answer] in facts for q in qs)
    assert not any("97.3" in o for q in qs for o in q.options)
    assert {q.section_id for q in qs} >= {"s1", "s2", "s3"}  # covers the whole paper
    assert [q.id for q in qs] == [f"q{k}" for k in range(1, len(qs) + 1)]


def test_quiz_links_questions_to_beats_and_quotes(tmp_path):
    qs = _run(_ctx(tmp_path, FakeLLM("mixed"))).questions
    for q in qs:
        word = re.search(r"the (\w+) in this", q.question).group(1)
        assert q.beat_id and q.beat_id.startswith(f"deep.{q.section_id}.")
        assert word in q.beat_id or word in _beat_text(q.beat_id)
        assert q.quote and word in q.quote


def _beat_text(beat_id: str) -> str:
    _, _, exp = _paper_and_views()
    return next(b.narration for s in exp.views["deep"].sections for b in s.beats if b.id == beat_id)


def test_quiz_tops_up_when_checks_drop_too_many(tmp_path):
    llm = FakeLLM("mostly_disputed")
    qs = _run(_ctx(tmp_path, llm)).questions
    assert len(qs) >= 10
    assert any("Already asked" in p for p in llm.prompts)


def test_quiz_selection_picks_the_final_set(tmp_path):
    llm = FakeLLM("clean")
    qs = _run(_ctx(tmp_path, llm)).questions
    listed = re.findall(r"^c\d+ \[", next(p for p in llm.prompts if "TASK: quiz_select" in p), re.M)
    assert len(listed) == 13 and len(qs) == 10  # every verified candidate was offered; 10 were kept
    asked = " ".join(q.question for q in qs)
    assert all(f"the {w} in" not in asked for w in ("routing", "router", "capacity"))  # the first three were left out
    assert [q.id for q in qs] == [f"q{k}" for k in range(1, 11)]


def test_quiz_selection_failure_falls_back_to_round_robin(tmp_path):
    qs = _run(_ctx(tmp_path, FakeLLM("clean", select="bad"))).questions
    assert len(qs) == 10
    assert {q.section_id for q in qs} == {"s0", "s1", "s2", "s3"}


def test_quiz_spreads_answers_over_positions(tmp_path):
    qs = _run(_ctx(tmp_path, FakeLLM("mixed"), quiz_questions=12)).questions
    assert len(qs) == 12
    counts = Counter(q.answer for q in qs)
    assert set(counts) == {0, 1, 2, 3} and max(counts.values()) - min(counts.values()) <= 1


def test_quiz_can_be_disabled(tmp_path):
    assert _run(_ctx(tmp_path, FakeLLM(), quiz=False)).questions == []


@pytest.mark.parametrize("raw,expected", [
    ("C", 2), ("c)", 2), (" b. ", 1), ("(A) the router", 0), ("Option D", 3), ("D: it caps tokens", 3),
    ("none", -1), (None, -1), ("E", -1), ("A model", -1), ("AB", -1),
])
def test_option_letters(raw, expected):
    assert quiz._index(raw) == expected


def test_question_problems():
    src = "The router reaches 41.2 accuracy."
    ok = quiz.QuestionOut(question="Q?", correct="41.2 accuracy", distractors=["a", "b", "c"], explanation="It says 41.2.")
    assert quiz._problems(ok, src) == []
    bad_num = ok.model_copy(update={"correct": "97.3 accuracy"})
    assert "numbers not in the paper" in quiz._problems(bad_num, src)[0]
    dup = ok.model_copy(update={"distractors": ["a", "a", "c"]})
    assert quiz._problems(dup, src)
    above = ok.model_copy(update={"distractors": ["a", "b", "All of the above"]})
    assert quiz._problems(above, src)
