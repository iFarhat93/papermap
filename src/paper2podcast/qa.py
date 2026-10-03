"""Grounded Q&A over a generated experience: BM25 retrieval over the paper
text + section notes + knowledge-graph facts, answers cite sections as [sN]."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .llm import LLMClient, Message
from .log import get_logger
from .models import Experience
from .retrieval import BM25
from .stages.base import SYSTEM_PROMPT

log = get_logger("qa")

QA_PROMPT = """TASK: answer

Answer the listener's question about the paper, using only the section notes, excerpts and knowledge-graph facts above.

THE LISTENER
{profile}

WHERE THEY ARE: {where}

Rules:
- Ground every claim in the material above and cite the section right after the claim, like [s3]. Use only these section ids: {ids}.
- If the paper does not answer the question, say so plainly in one sentence, then give the closest thing it does say (cited). Never guess.
- Be concise: 2-6 sentences unless they ask for detail. Match their expertise; skip what they already know.
- You may use **bold** for key terms, short "- " bullet lists and $...$ for math. No headings.

QUESTION: {question}"""


class QAEngine:
    def __init__(self, out_dir: Path, llm: LLMClient):
        self.out_dir = Path(out_dir)
        self.llm = llm
        self.exp = Experience.model_validate(json.loads((self.out_dir / "experience.json").read_text("utf-8")))
        index_path = self.out_dir / "qa_index.json"
        index = json.loads(index_path.read_text("utf-8")) if index_path.is_file() else {"chunks": [], "notes": {}}
        self.chunks: list[dict] = index["chunks"]
        self.notes: dict[str, dict] = index["notes"]
        self.titles = {s.id: s.title for s in self.exp.sections}
        self.bm25 = BM25([f"{c['title']} {c['text']}" for c in self.chunks])
        self.node_by_id = {n.id: n for n in self.exp.graph.nodes}

    # ------------------------------------------------------------- context
    def _diagram(self, diagram_id: str | None) -> dict | None:
        if not diagram_id:
            return None
        for view in self.exp.views.values():
            for sv in view.sections:
                if sv.diagram and sv.diagram.id == diagram_id:
                    return sv.diagram.model_dump(exclude_defaults=True)
        return None

    def _graph_facts(self, question: str, node_id: str | None, limit: int = 14) -> list[str]:
        q = question.lower()
        focus = set()
        if node_id:
            focus.add(node_id)
        for n in self.exp.graph.nodes:
            if any(len(x) > 2 and x.lower() in q for x in [n.label, *n.aliases]):
                focus.add(n.id)
        facts = []
        for e in self.exp.graph.edges:
            if focus and not ({e.source, e.target} & focus):
                continue
            a, b = self.node_by_id[e.source], self.node_by_id[e.target]
            secs = ",".join(e.sections)
            facts.append(f"{a.label} --{e.relation}--> {b.label}" + (f": {e.description}" if e.description else "") + f" [{secs}]")
            if len(facts) >= limit:
                break
        for nid in sorted(focus):
            n = self.node_by_id.get(nid)
            if n and n.description:
                facts.append(f"{n.label} ({n.type}): {n.description} [{','.join(n.sections)}]")
        return facts

    def build_context(self, question: str, ctx: dict[str, Any]) -> tuple[str, str]:
        section_id = ctx.get("section_id")
        node = self.node_by_id.get(ctx.get("node_id") or "")
        query = " ".join(x for x in [question, self.titles.get(section_id or "", ""), node.label if node else ""] if x)
        scores = self.bm25.scores(query)
        hits = sorted((i for i, sc in enumerate(scores) if sc > 0), key=lambda i: (-scores[i], i))[:6]
        if section_id:  # always include the best chunk of the section they are looking at
            local = [i for i, c in enumerate(self.chunks) if c["section"] == section_id]
            if local and not set(local) & set(hits):
                best = max(local, key=lambda i: (scores[i], -i))
                hits = [best] + hits[:5]
        notes = "\n".join(
            f"[{sid}] {n['title']}: {n['summary']} Key points: " + " | ".join(n["key_points"][:4])
            for sid, n in self.notes.items()
        )
        excerpts = "\n\n".join(f"[{self.chunks[i]['section']}] ({self.chunks[i]['title']})\n{self.chunks[i]['text']}" for i in hits)
        parts = [f"SECTION NOTES:\n{notes}", f"EXCERPTS FROM THE PAPER:\n{excerpts or '(no matching excerpt)'}"]
        facts = self._graph_facts(question, node.id if node else None)
        if facts:
            parts.append("KNOWLEDGE GRAPH FACTS:\n" + "\n".join(f"- {f}" for f in facts))
        diagram = self._diagram(ctx.get("diagram_id"))
        if diagram:
            parts.append("DIAGRAM THE LISTENER IS ASKING ABOUT:\n" + json.dumps(diagram, ensure_ascii=False))
        where = []
        if ctx.get("view"):
            where.append(f"{ctx['view']} view")
        if section_id in self.titles:
            where.append(f'section [{section_id}] "{self.titles[section_id]}"')
        if diagram:
            where.append(f'looking at the diagram "{diagram.get("title", "")}"')
        if node:
            where.append(f'asking about the graph node "{node.label}"')
        context = f'PAPER: "{self.exp.paper.title}"\n\n' + "\n\n".join(parts)
        return context, (", ".join(where) or "finished the paper")

    # -------------------------------------------------------------- answer
    def answer(self, question: str, ctx: dict[str, Any] | None = None, history: list[dict] | None = None) -> dict:
        question = question.strip()[:2000]
        if not question:
            return {"answer": "Ask me anything about the paper.", "refs": []}
        ctx = ctx or {}
        context, where = self.build_context(question, ctx)
        prompt = QA_PROMPT.format(profile=self.exp.profile.brief(), where=where, ids=", ".join(self.titles), question=question)
        messages: list[Message] = []
        for turn in (history or [])[-6:]:
            if turn.get("role") in ("user", "assistant") and turn.get("content"):
                messages.append(Message(turn["role"], str(turn["content"])[:3000]))
        if messages and messages[0].role != "user":
            messages = messages[1:]
        if messages and messages[-1].role == "user":
            messages = messages[:-1]
        messages.append(Message("user", prompt))
        text = self.llm.complete(messages, system=SYSTEM_PROMPT, context=context, max_tokens=1500, tag="qa").strip()
        refs: list[str] = []
        for group in re.findall(r"\[([^\]]{1,40})\]", text):  # "[s3]", "[s2, s5]"
            for sid in re.findall(r"\bs\d+\b", group):
                if sid in self.titles and sid not in refs:
                    refs.append(sid)
        return {"answer": text, "refs": refs, "titles": {r: self.titles[r] for r in refs}}
