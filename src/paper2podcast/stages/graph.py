"""Stage 6 - graph: a knowledge graph of how the paper relates to prior work,
models, datasets, tasks, metrics and concepts. Per-section extraction, then a
deterministic merge (alias resolution, grounding filter, pruning)."""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from pydantic import Field

from ..grounding import find_quote, lower_text, name_in_text
from ..llm import LLMOutputError
from ..models import (
    KG_NODE_TYPES,
    KG_RELATIONS,
    KGEdge,
    KGNode,
    KnowledgeGraph,
    Model,
    ParsedPaper,
    Understanding,
    choose,
)
from .base import SYSTEM_PROMPT, RunContext, Stage
from .common import context_block, grounding_text


class EntityOut(Model):
    name: str
    type: str = "concept"
    description: str = ""
    evidence: str = ""


class RelationOut(Model):
    source: str
    target: str
    relation: str = "related_to"
    description: str = ""


class GraphOut(Model):
    entities: list[EntityOut] = Field(default_factory=list)
    relations: list[RelationOut] = Field(default_factory=list)


GRAPH_PROMPT = """TASK: graph

Extract a knowledge graph of how the section "{title}" of the paper above connects to existing work and concepts.
The paper's own proposed method is called "{center}": always use exactly that name for it.

Return JSON:
{{"entities": [{{"name": "...", "type": "method | model | dataset | task | metric | concept | prior_work | tool", "description": "one sentence grounded in the text", "evidence": "a short phrase copied exactly from the text that mentions it"}}],
 "relations": [{{"source": "entity name", "target": "entity name", "relation": "{relations}", "description": "a few words"}}]}}

Rules:
- Only entities explicitly named in the section text (max 12). Prefer prior methods and models, datasets, tasks, metrics and key concepts over generic words.
- Relations must be stated or clearly implied by the text. When a relation is about this paper's contribution, use "{center}" as the source.
- Use "prior_work" for a cited earlier paper or system that is not better described as a method, model or dataset."""

_CENTER_ALIASES = {"this work", "this paper", "our method", "our approach", "our model", "proposed method", "ours", "we"}


_CITATION = re.compile(r"\s*(\[[\d,;\s\-–]+\]|\(\s*(19|20)\d\d[a-z]?\s*\))")


def clean_label(name: str) -> str:
    """Drop citation markers: "ConvS2S [9]" -> "ConvS2S", "Luong et al. (2015)" -> "Luong et al."."""
    return re.sub(r"\s+", " ", _CITATION.sub("", name)).strip(" ,;")


def _canon(name: str) -> str:
    n = lower_text(clean_label(name))
    n = re.sub(r"^(the|a|an)\s+", "", n)
    n = re.sub(r"[^\w\s\-+#.]", " ", n)
    return re.sub(r"\s+", " ", n).strip(" .-")


# Generic trailing words: "WMT 2014 En-De dataset" and "WMT 2014 En-De translation task" are one entity.
_GENERIC_SUFFIX = re.compile(r"\s+(dataset|data set|data|corpus|benchmark|task|translation task|translation|model|architecture)$")


def _alias_keys(name: str) -> set[str]:
    base = _canon(name)
    keys = {base, re.sub(r"(?<=\w)(-to-| to )(?=\w)", "-", base)}  # "English-to-French" == "English-French"
    m = re.search(r"\(([^)]{2,24})\)", name)
    if m:
        keys.add(_canon(m.group(1)))
        keys.add(_canon(re.sub(r"\s*\([^)]*\)", "", name)))
    for k in list(keys):
        stripped = k
        while True:  # peel generic suffixes while at least two words remain
            nxt = _GENERIC_SUFFIX.sub("", stripped)
            if nxt == stripped or len(nxt.split()) < 2:
                break
            stripped = nxt
            keys.add(stripped)
    for k in list(keys):
        if k.endswith("s") and len(k) > 4 and not k.endswith("ss"):
            keys.add(k[:-1])
        keys.add(k.replace("-", " "))
    return {k for k in keys if k}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _canon(text)).strip("-")[:40] or "node"


def _relation_from_text(text: str) -> str:
    t = text.lower()
    for pattern, rel in (
        (r"outperform|surpass|beat", "outperforms"),
        (r"improv|better than|address(es)? (the )?limitation", "improves_on"),
        (r"extend|build|generaliz", "extends"),
        (r"compar|baseline|versus|vs\.?", "compares_to"),
        (r"evaluat|benchmark|tested on|test on", "evaluated_on"),
        (r"based on|inspired|follow", "based_on"),
        (r"use|leverag|adopt|employ|appl", "uses"),
        (r"alternative|instead|unlike|contrast", "alternative_to"),
    ):
        if re.search(pattern, t):
            return rel
    return "related_to"


class _UnionFind:
    def __init__(self):
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        self.parent[self.find(a)] = self.find(b)


def merge_graph(
    per_section: list[tuple[str, GraphOut, str]],
    center_label: str,
    full_text: str,
    extra_prior: list[tuple[str, str, str]],
    max_nodes: int,
) -> KnowledgeGraph:
    """Deterministically merge per-section extractions into one graph.

    per_section: (section_id, extraction, section_text)
    extra_prior: (section_id, name, relation text) from the understand stage
    """
    full_low = lower_text(full_text)
    center_keys = _alias_keys(center_label) | _CENTER_ALIASES

    mentions: list[dict] = []  # each: name, type, description, evidence, section
    for sid, g, text in per_section:
        for e in g.entities:
            name = clean_label(e.name)
            if len(name) < 2:
                continue
            ev = find_quote(e.evidence, text) if e.evidence else None
            mentions.append({"name": name, "type": e.type, "description": e.description.strip(), "evidence": ev or "", "section": sid})
    for sid, name, _ in extra_prior:
        if len(clean_label(name)) >= 2:
            mentions.append({"name": clean_label(name), "type": "prior_work", "description": "", "evidence": "", "section": sid})
    mentions.append({"name": center_label, "type": "this_work", "description": "", "evidence": "", "section": "s0"})

    # union mentions sharing an alias key
    uf = _UnionFind()
    key_owner: dict[str, int] = {}
    for i, m in enumerate(mentions):
        keys = _alias_keys(m["name"])
        if keys & center_keys:
            keys |= {"__center__"}
        for k in keys:
            if k in key_owner:
                uf.union(i, key_owner[k])
            else:
                key_owner[k] = i
    clusters: dict[int, list[int]] = defaultdict(list)
    for i in range(len(mentions)):
        clusters[uf.find(i)].append(i)

    nodes: dict[int, KGNode] = {}
    used_ids: set[str] = set()
    center_root = uf.find(len(mentions) - 1)
    for root, idxs in clusters.items():
        ms = [mentions[i] for i in idxs]
        is_center = root == center_root
        names = Counter(m["name"] for m in ms)
        label = center_label if is_center else max(names, key=lambda n: (names[n], len(n)))
        types = Counter(choose(m["type"], KG_NODE_TYPES, "concept") for m in ms)
        if is_center:
            ntype = "this_work"
        else:
            types.pop("this_work", None)
            non_concept = {t: c for t, c in types.items() if t != "concept"}
            ntype = max(non_concept, key=non_concept.get) if non_concept else "concept"
        aliases = sorted({m["name"] for m in ms} - {label})[:6]
        evidence = next((m["evidence"] for m in ms if m["evidence"]), "")
        grounded = is_center or bool(evidence) or any(name_in_text(n, full_low) for n in names)
        if not grounded:
            continue
        nid = "this-work" if is_center else _slug(label)
        base, k = nid, 2
        while nid in used_ids:
            nid = f"{base}-{k}"
            k += 1
        used_ids.add(nid)
        nodes[root] = KGNode(
            id=nid,
            label=label,
            type=ntype,
            description=next((m["description"] for m in ms if m["description"]), ""),
            sections=sorted({m["section"] for m in ms}, key=lambda s: int(s[1:]) if s[1:].isdigit() else 0),
            evidence=evidence,
            aliases=aliases,
            mentions=len(ms),
        )

    def lookup(name: str) -> KGNode | None:
        keys = _alias_keys(name)
        if keys & center_keys:
            return nodes.get(center_root)
        for k in keys:
            if k in key_owner:
                n = nodes.get(uf.find(key_owner[k]))
                if n:
                    return n
        return None

    edges: dict[tuple[str, str, str], KGEdge] = {}
    for sid, g, _ in per_section:
        for r in g.relations:
            a, b = lookup(r.source), lookup(r.target)
            if not a or not b or a.id == b.id:
                continue
            rel = choose(r.relation, KG_RELATIONS, _relation_from_text(r.relation))
            key = (a.id, b.id, rel)
            if key in edges:
                if sid not in edges[key].sections:
                    edges[key].sections.append(sid)
            else:
                edges[key] = KGEdge(source=a.id, target=b.id, relation=rel, description=r.description.strip(), sections=[sid])
    center = nodes[center_root]
    for sid, name, relation in extra_prior:
        n = lookup(name)
        if n and n.id != center.id and not any(e for e in edges.values() if {e.source, e.target} == {center.id, n.id}):
            rel = _relation_from_text(relation)
            edges[(center.id, n.id, rel)] = KGEdge(source=center.id, target=n.id, relation=rel, description=relation, sections=[sid])

    # prune to the most connected / most mentioned nodes; isolated nodes are clutter
    degree: Counter[str] = Counter()
    for e in edges.values():
        degree[e.source] += 1
        degree[e.target] += 1
    candidates = list(nodes.values())
    if len(candidates) > 12:
        candidates = [n for n in candidates if n.id == center.id or degree[n.id] > 0]
    ranked = sorted(candidates, key=lambda n: (n.id != center.id, -(degree[n.id] * 2 + n.mentions), n.label.lower()))
    keep = {n.id for n in ranked[:max_nodes]}
    final_nodes = [n for n in ranked if n.id in keep]
    final_edges = [e for e in edges.values() if e.source in keep and e.target in keep]
    final_nodes.sort(key=lambda n: (n.id != center.id, n.type, n.label.lower()))
    final_edges.sort(key=lambda e: (e.source, e.target, e.relation))
    return KnowledgeGraph(center=center.id, nodes=final_nodes, edges=final_edges)


def _run(ctx: RunContext, deps: dict) -> KnowledgeGraph:
    log = STAGE.log()
    paper: ParsedPaper = deps["parse"]
    und: Understanding = deps["understand"]
    llm = ctx.llm("graph")
    cfg = ctx.config.pipeline
    center = und.overview.method_name or "This work"

    def extract(su):
        text = grounding_text(paper, und, su.id, cfg.max_section_chars)
        try:
            g = llm.complete_json(
                GRAPH_PROMPT.format(title=su.title, center=center, relations=" | ".join(KG_RELATIONS)),
                GraphOut,
                system=SYSTEM_PROMPT,
                context=context_block(und.overview.title, f"SECTION TEXT ({su.title})", text),
                tag=f"graph.{su.id}",
            )
        except LLMOutputError as e:
            log.warning("%s: graph extraction failed (%s); skipping section", su.id, e)
            g = GraphOut()
        return su.id, g, text

    per_section = ctx.parallel(extract, und.sections)
    extra = [(s.id, p.name, p.relation) for s in und.sections for p in s.prior_work]
    kg = merge_graph(per_section, center, paper.full_text, extra, cfg.max_graph_nodes)
    types = Counter(n.type for n in kg.nodes)
    log.info("%d nodes, %d edges %s", len(kg.nodes), len(kg.edges), dict(types))
    return kg


STAGE = Stage(
    name="graph",
    version="1",
    deps=("parse", "understand"),
    output=KnowledgeGraph,
    run=_run,
    key_extra=lambda ctx: {"max_nodes": ctx.config.pipeline.max_graph_nodes, "chars": ctx.config.pipeline.max_section_chars},
    description="knowledge graph of prior work, methods, datasets and concepts linked to sections",
)
