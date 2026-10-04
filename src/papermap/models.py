"""The data contract shared by every pipeline stage and the generated frontend.

Each stage consumes and produces these models. They are serialized to JSON in
the stage cache, in ``<out>/debug/`` and (assembled) in ``experience.json``,
which is the single artifact the webpage renders. Changing a field here is a
contract change: bump ``SCHEMA_VERSION`` and the affected stage versions.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1"

# Section ids: "s0" is the synthetic overview, "s1".."sN" the logical sections.
OVERVIEW_ID = "s0"

VIEW_KINDS = ("high", "deep")

SECTION_ROLES = (
    "overview", "motivation", "background", "related_work", "method", "theory",
    "experiments", "results", "analysis", "discussion", "limitations",
    "conclusion", "appendix", "other",
)

DIAGRAM_TYPES = ("flow", "bar", "table", "equation")

NODE_KINDS = ("input", "process", "model", "data", "output", "metric", "concept", "decision")

KG_NODE_TYPES = (
    "this_work", "method", "model", "dataset", "task", "metric", "concept",
    "prior_work", "tool",
)

KG_RELATIONS = (
    "introduces", "extends", "improves_on", "outperforms", "compares_to", "uses",
    "based_on", "evaluated_on", "measured_by", "addresses", "part_of",
    "alternative_to", "related_to",
)


def choose(value: Any, allowed: tuple[str, ...], default: str) -> str:
    """Coerce a free-form LLM label onto a closed vocabulary."""
    if not isinstance(value, str):
        return default
    v = value.strip().lower().replace("-", "_").replace(" ", "_")
    if v in allowed:
        return v
    for a in allowed:  # tolerate plurals / prefixes such as "methods", "result"
        if v.startswith(a) or a.startswith(v) and len(v) >= 4:
            return a
    return default


class Model(BaseModel):
    # LLM output is noisy: ignore unknown keys rather than failing validation.
    model_config = ConfigDict(extra="ignore")


# --------------------------------------------------------------------- parse


class RawSection(Model):
    id: str
    heading: str
    level: int = 1
    text: str
    page_start: int
    page_end: int


class Figure(Model):
    id: str
    caption: str
    page: int = 0
    number: int = 0
    files: list[str] = Field(default_factory=list)  # rendered PNGs, relative to the cache root
    raw_section_id: str | None = None
    sub_captions: list[str] = Field(default_factory=list)


class Table(Model):
    id: str
    number: int = 0
    caption: str = ""
    columns: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    page: int | None = None
    raw_section_id: str | None = None

    def as_text(self) -> str:
        lines = [" | ".join(self.columns)] + [" | ".join(r) for r in self.rows]
        return f"Table {self.number}: {self.caption}\n" + "\n".join(lines)


class ParsedPaper(Model):
    source: str
    sha256: str
    title: str
    first_page_text: str
    n_pages: int
    sections: list[RawSection]
    references: list[str] = Field(default_factory=list)
    figures: list[Figure] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    pdf_path: str = ""  # local PDF used for page images and source highlighting
    source_kind: str = "pdf"  # "pdf" | "latex"
    arxiv_id: str = ""

    @property
    def full_text(self) -> str:
        return "\n\n".join(f"{s.heading}\n{s.text}" for s in self.sections)

    def section(self, raw_id: str) -> RawSection | None:
        return next((s for s in self.sections if s.id == raw_id), None)


# ---------------------------------------------------------------- understand


class Equation(Model):
    latex: str
    meaning: str = ""


class ResultItem(Model):
    claim: str
    metric: str = ""
    value: str = ""
    baseline: str = ""


class Concept(Model):
    name: str
    explanation: str = ""


class PriorWork(Model):
    name: str
    relation: str = ""


class SectionUnderstanding(Model):
    id: str
    title: str
    role: str = "other"
    raw_section_ids: list[str] = Field(default_factory=list)
    pages: list[int] = Field(default_factory=list)
    summary: str
    key_points: list[str] = Field(default_factory=list)
    key_quotes: list[str] = Field(default_factory=list)  # verified verbatim
    equations: list[Equation] = Field(default_factory=list)
    results: list[ResultItem] = Field(default_factory=list)
    concepts: list[Concept] = Field(default_factory=list)
    prior_work: list[PriorWork] = Field(default_factory=list)

    @field_validator("role", mode="before")
    @classmethod
    def _role(cls, v: Any) -> str:
        return choose(v, SECTION_ROLES, "other")


class PaperOverview(Model):
    title: str
    authors: list[str] = Field(default_factory=list)
    method_name: str = ""
    one_line: str
    problem: str
    contribution: str
    key_result: str = ""


class Understanding(Model):
    overview: PaperOverview
    sections: list[SectionUnderstanding]

    def section(self, section_id: str) -> SectionUnderstanding | None:
        return next((s for s in self.sections if s.id == section_id), None)


# ------------------------------------------------------------------- profile


class AudienceProfile(Model):
    name: str = "Reader"
    summary: str = "A curious reader."
    expertise_level: str = "intermediate"
    background: list[str] = Field(default_factory=list)
    known_concepts: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)
    goals: list[str] = Field(default_factory=list)
    depth: str = "balanced"
    tone: str = "warm, precise, conversational"
    language: str = "English"
    language_code: str = "en-US"
    analogy_domains: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)

    @field_validator("expertise_level", mode="before")
    @classmethod
    def _level(cls, v: Any) -> str:
        return choose(v, ("novice", "intermediate", "advanced", "expert"), "intermediate")

    @field_validator("depth", mode="before")
    @classmethod
    def _depth(cls, v: Any) -> str:
        return choose(v, ("light", "balanced", "deep"), "balanced")

    def brief(self) -> str:
        """Compact rendering used inside prompts."""
        parts = [
            f"Name: {self.name}",
            f"Who they are: {self.summary}",
            f"Expertise: {self.expertise_level}; preferred depth: {self.depth}",
        ]
        if self.background:
            parts.append("Background: " + "; ".join(self.background))
        if self.known_concepts:
            parts.append("Already knows (do not re-explain): " + "; ".join(self.known_concepts))
        if self.gaps:
            parts.append("Likely unfamiliar with (explain briefly when needed): " + "; ".join(self.gaps))
        if self.interests:
            parts.append("Interests: " + "; ".join(self.interests))
        if self.goals:
            parts.append("Goals for this paper: " + "; ".join(self.goals))
        if self.analogy_domains:
            parts.append("Good analogy domains: " + "; ".join(self.analogy_domains))
        if self.avoid:
            parts.append("Avoid: " + "; ".join(self.avoid))
        parts.append(f"Tone: {self.tone}")
        parts.append(f"Language: {self.language}")
        return "\n".join(parts)


# ------------------------------------------------------------------- explain


class SourceRef(Model):
    """Where a beat comes from in the PDF: a page and (for quotes) highlight boxes."""

    page: int
    rects: list[list[float]] = Field(default_factory=list)  # [x0, y0, x1, y1], fractions of the page


class Beat(Model):
    """One narrated step: a few spoken sentences plus what is on screen."""

    id: str
    speaker: str = "narrator"
    narration: str
    subtitle: str
    quote: str | None = None
    focus: list[str] = Field(default_factory=list)  # diagram element ids to highlight
    refs: list[str] = Field(default_factory=list)  # section ids this beat draws on
    audio: str | None = None  # relative path, filled by the narrate stage
    source: SourceRef | None = None  # filled by the render stage


class DiagramBrief(Model):
    needed: bool = False
    type: str = "flow"
    brief: str = ""

    @field_validator("type", mode="before")
    @classmethod
    def _type(cls, v: Any) -> str:
        return choose(v, DIAGRAM_TYPES, "flow")


class SectionView(Model):
    section_id: str
    title: str
    beats: list[Beat]
    diagram_brief: DiagramBrief = Field(default_factory=DiagramBrief)
    diagram: Diagram | None = None


class View(Model):
    kind: str
    sections: list[SectionView]

    def section(self, section_id: str) -> SectionView | None:
        return next((s for s in self.sections if s.section_id == section_id), None)


class ReviewIssue(Model):
    view: str
    section_id: str
    beat: str
    problem: str
    fix: str = ""
    applied: bool = False


class Explanations(Model):
    views: dict[str, View]
    suggested_questions: list[str] = Field(default_factory=list)
    review_issues: list[ReviewIssue] = Field(default_factory=list)


# ------------------------------------------------------------------ diagrams


class DiagramNode(Model):
    id: str
    label: str
    detail: str = ""
    kind: str = "process"
    group: str | None = None

    @field_validator("kind", mode="before")
    @classmethod
    def _kind(cls, v: Any) -> str:
        return choose(v, NODE_KINDS, "process")

    @field_validator("id", "label", mode="before")
    @classmethod
    def _str(cls, v: Any) -> str:
        return str(v).strip()

    @field_validator("group", mode="before")
    @classmethod
    def _group(cls, v: Any) -> str | None:
        if v is None or str(v).strip().lower() in ("", "null", "none", "n/a"):
            return None
        return str(v).strip()


class DiagramEdge(Model):
    source: str
    target: str
    label: str = ""


class DiagramGroup(Model):
    id: str
    label: str


_SCI = re.compile(r"^\s*([-+]?\d*\.?\d+)\s*(?:[x×*·⋅]\s*10\s*\^?\s*\(?\s*([-+−]?\d+)\s*\)?)?\s*%?\s*$")


def parse_number(v: Any) -> float | None:
    """Accept 28.4, "28.4", "28.4%", "1,024", "3.3e18", "3.3 × 10^18" and LaTeX "$3.3 \\cdot 10^{18}$"."""
    if v is None or isinstance(v, (int, float)):
        return v
    s = str(v).strip().replace(",", "")
    if not s or s.lower() in ("null", "none", "n/a", "-", "–"):
        return None
    try:
        return float(s.rstrip("%"))
    except ValueError:
        pass
    s = re.sub(r"\\(?:cdot|times)", "×", s.replace("$", "")).replace("{", "").replace("}", "")
    m = _SCI.match(s)
    if not m:
        raise ValueError(f"not a number: {v!r}")
    mant = float(m.group(1))
    return mant * 10 ** int(m.group(2).replace("−", "-")) if m.group(2) else mant


class Series(Model):
    name: str
    values: list[float | None]

    @field_validator("values", mode="before")
    @classmethod
    def _values(cls, v: Any) -> Any:
        return [parse_number(x) for x in v] if isinstance(v, list) else v


class Term(Model):
    symbol: str
    meaning: str


class Diagram(Model):
    id: str
    type: str
    title: str
    caption: str = ""
    direction: str = "LR"
    # flow
    nodes: list[DiagramNode] = Field(default_factory=list)
    edges: list[DiagramEdge] = Field(default_factory=list)
    groups: list[DiagramGroup] = Field(default_factory=list)
    # bar
    categories: list[str] = Field(default_factory=list)
    series: list[Series] = Field(default_factory=list)
    unit: str = ""
    higher_is_better: bool | None = None
    # table
    columns: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    # equation
    latex: str = ""
    terms: list[Term] = Field(default_factory=list)

    @field_validator("type", mode="before")
    @classmethod
    def _type(cls, v: Any) -> str:
        return choose(v, DIAGRAM_TYPES, "flow")

    @field_validator("direction", mode="before")
    @classmethod
    def _dir(cls, v: Any) -> str:
        return "TB" if str(v).upper() in ("TB", "TD", "BT", "VERTICAL") else "LR"

    @field_validator("rows", mode="before")
    @classmethod
    def _rows(cls, v: Any) -> Any:
        if isinstance(v, list):
            return [[("" if c is None else str(c)) for c in row] for row in v if isinstance(row, list)]
        return v

    def element_ids(self) -> list[str]:
        """Ids a beat may put in ``focus`` for this diagram."""
        if self.type == "flow":
            return [n.id for n in self.nodes] + [g.id for g in self.groups]
        if self.type == "bar":
            return list(self.categories) + [s.name for s in self.series]
        if self.type == "table":
            return [r[0] for r in self.rows if r] + list(self.columns)
        if self.type == "equation":
            return [t.symbol for t in self.terms]
        return []

    def problems(self) -> list[str]:
        """Structural issues; an empty list means the diagram is renderable."""
        p: list[str] = []
        if self.type == "flow":
            ids = [n.id for n in self.nodes]
            if len(self.nodes) < 2:
                p.append("a flow diagram needs at least 2 nodes")
            dup = {i for i in ids if ids.count(i) > 1}
            if dup:
                p.append(f"duplicate node ids: {sorted(dup)}")
            known = set(ids)
            for e in self.edges:
                if e.source not in known or e.target not in known:
                    p.append(f"edge {e.source}->{e.target} references an unknown node id")
            groups = {g.id for g in self.groups}
            for n in self.nodes:
                if n.group and n.group not in groups:
                    p.append(f"node {n.id} uses unknown group {n.group!r}")
        elif self.type == "bar":
            if len(self.categories) < 2 or not self.series:
                p.append("a bar chart needs at least 2 categories and 1 series")
            for s in self.series:
                if len(s.values) != len(self.categories):
                    p.append(f"series {s.name!r} has {len(s.values)} values for {len(self.categories)} categories")
        elif self.type == "table":
            if len(self.columns) < 2 or not self.rows:
                p.append("a table needs at least 2 columns and 1 row")
            for r in self.rows:
                if len(r) != len(self.columns):
                    p.append(f"row {r[:1]} has {len(r)} cells for {len(self.columns)} columns")
        elif self.type == "equation":
            if not self.latex.strip():
                p.append("an equation diagram needs latex")
        return p


class Diagrams(Model):
    # key: "<view>:<section_id>"
    diagrams: dict[str, Diagram] = Field(default_factory=dict)
    # beat id -> diagram element ids to highlight while that beat plays
    focus: dict[str, list[str]] = Field(default_factory=dict)


# ----------------------------------------------------------- knowledge graph


class KGNode(Model):
    id: str
    label: str
    type: str = "concept"
    description: str = ""
    sections: list[str] = Field(default_factory=list)
    evidence: str = ""
    aliases: list[str] = Field(default_factory=list)
    mentions: int = 1

    @field_validator("type", mode="before")
    @classmethod
    def _type(cls, v: Any) -> str:
        return choose(v, KG_NODE_TYPES, "concept")


class KGEdge(Model):
    source: str
    target: str
    relation: str = "related_to"
    description: str = ""
    sections: list[str] = Field(default_factory=list)

    @field_validator("relation", mode="before")
    @classmethod
    def _rel(cls, v: Any) -> str:
        return choose(v, KG_RELATIONS, "related_to")


class KnowledgeGraph(Model):
    center: str
    nodes: list[KGNode] = Field(default_factory=list)
    edges: list[KGEdge] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> KnowledgeGraph:
        ids = {n.id for n in self.nodes}
        self.edges = [e for e in self.edges if e.source in ids and e.target in ids]
        return self


# ----------------------------------------------------------------- narration


class NarrationInfo(Model):
    provider: str = "browser"  # browser | none | openai | edge
    language_code: str = "en-US"
    voices: dict[str, str] = Field(default_factory=dict)  # speaker -> voice
    rate: float = 1.0


class Narration(Model):
    info: NarrationInfo
    clips: dict[str, str] = Field(default_factory=dict)  # beat id -> relative audio path


# ---------------------------------------------------------------- experience


class FigureRef(Model):
    id: str
    number: int = 0
    caption: str = ""
    images: list[str] = Field(default_factory=list)  # relative paths in the output folder
    sub_captions: list[str] = Field(default_factory=list)
    page: int | None = None


class SectionMeta(Model):
    id: str
    title: str
    role: str
    pages: list[int] = Field(default_factory=list)
    summary: str = ""
    figures: list[FigureRef] = Field(default_factory=list)


class PageImage(Model):
    src: str
    width: float
    height: float


class PaperMeta(Model):
    title: str
    authors: list[str] = Field(default_factory=list)
    source: str
    n_pages: int
    method_name: str = ""
    one_line: str = ""
    problem: str = ""
    contribution: str = ""
    key_result: str = ""
    source_kind: str = "pdf"
    arxiv_id: str = ""
    pages: list[PageImage] = Field(default_factory=list)  # rendered PDF pages (for the source viewer)


class QAInfo(Model):
    endpoint: str = "api/ask"
    suggested_questions: list[str] = Field(default_factory=list)


class QuizQuestion(Model):
    """One multiple-choice comprehension question with its answer key."""

    id: str
    section_id: str
    question: str
    options: list[str]
    answer: int  # index into options
    explanation: str
    quote: str = ""  # verified sentence from the paper supporting the answer
    kind: str = "concept"
    difficulty: str = "medium"
    beat_id: str | None = None  # the narration beat that covers it (for "review this")
    source: SourceRef | None = None  # where the quote is in the PDF (filled by the render stage)


class Quiz(Model):
    questions: list[QuizQuestion] = Field(default_factory=list)


class Experience(Model):
    """Everything the webpage needs, in one document."""

    schema_version: str = SCHEMA_VERSION
    generator: str
    fingerprint: str
    paper: PaperMeta
    profile: AudienceProfile
    sections: list[SectionMeta]
    views: dict[str, View]
    graph: KnowledgeGraph
    narration: NarrationInfo
    qa: QAInfo
    quiz: Quiz = Field(default_factory=Quiz)


SectionView.model_rebuild()
