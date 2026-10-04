"""Reader memory: what PaperMap learns about a reader across papers, kept on this computer.

Pages opened with `papermap serve` report what the reader does: quiz answers, questions
asked, papers finished and a one-click rating of the level. Each becomes an event in a
per-reader log in the local data folder (not the cache, and never inside a generated
page). Before a run, the log is consolidated in code, with recent evidence weighing more,
into the concepts the reader has shown they understand, the ones they struggled with, the
papers they read and a level adjustment. The profile stage adds that to the reader model,
so each new explanation builds on the earlier ones and gets more precise with use.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

from pydantic import Field

from .config import stable_hash
from .grounding import canon
from .models import AudienceProfile, Experience, KnowledgeGraph, Model

# Graph node types that are ideas a reader can learn (not prior works, datasets or tools).
CONCEPT_TYPES = ("this_work", "method", "concept", "metric")
# Evidence per event; it fades by DECAY for every paper read since.
EVIDENCE = {"correct": 1.0, "missed": -1.5, "asked": -0.5, "seen": 0.3}
DECAY = 0.8
MASTERED, STRUGGLES = 1.0, -1.0
# Words too generic to count as something a reader learned.
GENERIC = {"the", "this", "that", "these", "our", "their", "paper", "work", "method", "model", "approach", "results"}
LEVELS = {"too_basic": 1, "just_right": 0, "too_advanced": -1}
DEPTHS = ("light", "balanced", "deep")
EXPERTISE = ("novice", "intermediate", "advanced", "expert")


def default_memory_dir() -> Path:
    env = os.environ.get("PAPERMAP_MEMORY_DIR")
    if env:
        return Path(env)
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "papermap" / "readers"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "papermap" / "readers"


def reader_id(text: str) -> str:
    """A safe folder name for a reader (by default the profile file name)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text or "").strip("-.")[:60]


class MemoryStore:
    """One append-only JSON-lines event log per reader: easy to read, edit or delete."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else default_memory_dir()
        self._lock = threading.Lock()

    def path(self, reader: str) -> Path:
        rid = reader_id(reader)
        if not rid:
            raise ValueError("a reader id is required")
        return self.root / rid / "events.jsonl"

    def append(self, reader: str, event: dict) -> None:
        path = self.path(reader)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def events(self, reader: str) -> list[dict]:
        path = self.path(reader)
        if not path.is_file():
            return []
        out = []
        for line in path.read_text("utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue  # a hand-edited or half-written line
            if isinstance(e, dict):
                out.append(e)
        return out

    def readers(self) -> list[str]:
        return sorted(p.parent.name for p in self.root.glob("*/events.jsonl")) if self.root.is_dir() else []

    def forget(self, reader: str) -> bool:
        folder = self.path(reader).parent
        if not folder.is_dir():
            return False
        shutil.rmtree(folder)
        return True


# ---------------------------------------------------------------- consolidation


class ConceptState(Model):
    label: str
    score: float = 0.0
    correct: int = 0
    missed: int = 0
    asked: int = 0
    seen: int = 0


class MemorySummary(Model):
    """What the explanations use from a reader's memory."""

    reader: str = ""
    papers: int = 0
    mastered: list[str] = Field(default_factory=list)
    struggles: list[str] = Field(default_factory=list)
    history: list[str] = Field(default_factory=list)  # recent papers, with quiz scores
    level: float = 0.0  # mean of recent ratings: +1 too basic ... -1 too advanced
    concepts: dict[str, ConceptState] = Field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not (self.mastered or self.struggles or self.history or self.level)

    def digest(self) -> str:
        """Changes only when what the explanations see changes (not on every event)."""
        return stable_hash(self.mastered, self.struggles, self.history, round(self.level, 2))[:16]


def _evidence(kind: str, data: dict) -> list[tuple[str, list[str]]]:
    if kind == "quiz":
        return [("correct" if a.get("correct") else "missed", list(a.get("concepts") or []))
                for a in data.get("answers") or [] if isinstance(a, dict)]
    if kind == "ask":
        return [("asked", list(data.get("concepts") or []))]
    if kind == "finished":
        return [("seen", list(data.get("concepts") or []))]
    return []


def consolidate(events: list[dict], reader: str = "") -> MemorySummary:
    papers: list[str] = []
    for e in events:
        if e.get("paper") and e["paper"] not in papers:
            papers.append(e["paper"])
    rank = {p: i for i, p in enumerate(papers)}
    concepts: dict[str, ConceptState] = {}
    ratings: list[int] = []
    read: dict[str, dict] = {}  # paper -> title and quiz score, in reading order
    for e in events:
        kind, paper = e.get("kind"), e.get("paper", "")
        data = e.get("data") if isinstance(e.get("data"), dict) else {}
        weight = DECAY ** (len(papers) - 1 - rank.get(paper, len(papers) - 1))
        if kind == "level" and data.get("level") in LEVELS:
            ratings.append(LEVELS[data["level"]])
        if kind in ("finished", "quiz") and paper:
            entry = read.setdefault(paper, {"title": e.get("title") or paper, "quiz": ""})
            if kind == "quiz" and data.get("total"):
                entry["quiz"] = f"{data.get('correct', 0)}/{data['total']}"
        for outcome, labels in _evidence(kind, data):
            for label in labels:
                key = canon(str(label))
                if len(key) < 3 or key in GENERIC:
                    continue
                state = concepts.setdefault(key, ConceptState(label=str(label)))
                state.score += weight * EVIDENCE[outcome]
                setattr(state, outcome, getattr(state, outcome) + 1)
    by_score = sorted(concepts.values(), key=lambda s: s.score)
    recent = ratings[-3:]
    return MemorySummary(
        reader=reader,
        papers=len(read),
        mastered=[s.label for s in reversed(by_score) if s.score >= MASTERED][:10],
        struggles=[s.label for s in by_score if s.score <= STRUGGLES][:8],
        history=[f"{v['title']} (quiz {v['quiz']})" if v["quiz"] else v["title"] for v in list(read.values())[-5:]],
        level=sum(recent) / len(recent) if recent else 0.0,
        concepts=concepts,
    )


def _step(scale: tuple[str, ...], value: str, d: int) -> str:
    i = scale.index(value) if value in scale else 1
    return scale[max(0, min(len(scale) - 1, i + d))]


def apply_memory(profile: AudienceProfile, memory: MemorySummary | None, adjust_level: bool = True) -> AudienceProfile:
    """The reader model from profile.md, refined by what PaperMap has learned about them."""
    if memory is None or memory.empty:
        return profile
    p = profile.model_copy(deep=True)
    struggling = {canon(c) for c in memory.struggles}
    p.known_concepts = [c for c in p.known_concepts if canon(c) not in struggling]  # evidence beats self-report
    known = {canon(c) for c in p.known_concepts}
    p.mastered = [c for c in memory.mastered if canon(c) not in known]
    p.struggles = list(memory.struggles)
    p.history = list(memory.history)
    if adjust_level and abs(memory.level) >= 0.6:
        d = 1 if memory.level > 0 else -1
        p.depth = _step(DEPTHS, p.depth, d)
        p.expertise_level = _step(EXPERTISE, p.expertise_level, d)
        p.level_note = ("recent papers felt too basic: go deeper and skip more of the basics" if d > 0 else
                        "recent papers felt too advanced: slow down and explain more")
    return p


# ------------------------------------------------------------------ recording


def _learnable(n) -> bool:
    return n.type in CONCEPT_TYPES and canon(n.label) not in GENERIC


def concepts_in(text: str, graph: KnowledgeGraph, limit: int = 4) -> list[str]:
    """Graph concepts named in a piece of text (a question, a quiz item)."""
    hay = f" {canon(text)} "
    found: list[str] = []
    for n in sorted(graph.nodes, key=lambda n: -len(n.label)):
        if not _learnable(n):
            continue
        keys = {canon(x) for x in (n.label, *n.aliases)} - GENERIC
        if any(len(k) >= 3 and f" {k} " in hay for k in keys) and n.label not in found:
            found.append(n.label)
    return found[:limit]


def central_concepts(graph: KnowledgeGraph, limit: int = 6) -> list[str]:
    nodes = [n for n in graph.nodes if _learnable(n)]
    return [n.label for n in sorted(nodes, key=lambda n: (-n.mentions, -len(n.sections)))[:limit]]


def paper_key(exp: Experience) -> str:
    """Stable across copies and moves of the same paper."""
    return f"arXiv:{exp.paper.arxiv_id}" if exp.paper.arxiv_id else f"title:{canon(exp.paper.title)}"


class Recorder:
    """Turns what happens on a served page into memory events for its reader."""

    def __init__(self, store: MemoryStore, reader: str, exp: Experience):
        self.store, self.reader, self.exp = store, reader, exp
        self.paper = paper_key(exp)
        self.questions = {q.id: q for q in exp.quiz.questions}

    def _add(self, kind: str, data: dict) -> None:
        self.store.append(self.reader, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind,
                                        "paper": self.paper, "title": self.exp.paper.title, "data": data})

    def record(self, kind: str, data: dict) -> None:
        """Events posted by the page; correctness is re-derived here from the answer key."""
        if kind == "quiz":
            chosen = data.get("answers") if isinstance(data.get("answers"), dict) else {}
            rows = [{"q": qid, "concepts": q.concepts, "correct": chosen[qid] == q.answer}
                    for qid, q in self.questions.items() if isinstance(chosen.get(qid), int)]
            if not rows:
                raise ValueError("no quiz answers to record")
            self._add("quiz", {"correct": sum(r["correct"] for r in rows), "total": len(self.questions), "answers": rows})
        elif kind == "finished":
            self._add("finished", {"concepts": central_concepts(self.exp.graph)})
        elif kind == "level":
            if data.get("level") not in LEVELS:
                raise ValueError(f"level must be one of {', '.join(LEVELS)}")
            self._add("level", {"level": data["level"]})
        else:
            raise ValueError(f"unknown memory event {kind!r}")

    def asked(self, question: str, section_id: str | None = None) -> None:
        self._add("ask", {"question": question[:300], "section": section_id or "",
                          "concepts": concepts_in(question, self.exp.graph)})
