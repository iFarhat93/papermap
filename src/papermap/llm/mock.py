"""Deterministic offline provider.

Produces structurally valid, text-derived outputs for every pipeline task so
the whole pipeline (and the generated page) can run without any model: used
by the test suite and for UI development (``--provider mock``). Content is
extractive and simplistic by design - it is not an explainer.
"""

from __future__ import annotations

import json
import re

from .base import LLMProvider, LLMRequest, LLMResponse


def _sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text)
    return [p.strip() for p in parts if 40 <= len(p.strip()) <= 260]


def _section_text(context: str) -> str:
    m = re.search(r"<<<\n(.*?)\n>>>", context or "", re.S)
    return m.group(1) if m else (context or "")


def _numbers_with_labels(text: str, limit: int = 6) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    seen: set[str] = set()
    for m in re.finditer(r"([A-Za-z][A-Za-z\-]+(?: [A-Za-z][A-Za-z\-]+)?)[^\d\n]{0,12}?(\d{1,3}\.\d{1,2})", text):
        label, num = m.group(1).strip(), m.group(2)
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((label[:24], float(num)))
        if len(out) >= limit:
            break
    return out


class MockProvider(LLMProvider):
    name = "mock"
    supports_schema = False
    supports_json_mode = False

    def complete(self, request: LLMRequest) -> LLMResponse:
        prompt = request.messages[-1].content
        first = request.messages[0].content
        m = re.search(r"TASK: (\w+)", first) or re.search(r"TASK: (\w+)", prompt)
        task = m.group(1) if m else "chat"
        handler = getattr(self, f"_task_{task}", self._task_chat)
        out = handler(first, request.context or "")
        text = out if isinstance(out, str) else json.dumps(out)
        return LLMResponse(text=text, usage={"input_tokens": len(first) // 4, "output_tokens": len(text) // 4}, model="mock")

    # ------------------------------------------------------------ tasks
    def _task_plan(self, prompt: str, context: str):
        rows = re.findall(r"^(r\d+) \| (\s*)(.+?) \| p", prompt, re.M)
        title = re.search(r'PAPER: "(.*?)"', context)
        groups: list[list[tuple[str, str]]] = []
        for rid, indent, heading in rows:
            if heading.strip().lower() == "abstract":
                continue
            if not indent and (not groups or len(groups[-1]) >= 1):
                groups.append([])
            if not groups:
                groups.append([])
            groups[-1].append((rid, heading.strip()))
        while len(groups) > 6:  # merge the two smallest neighbours
            groups[-2].extend(groups.pop())
        sections = [
            {"title": re.sub(r"^[\dIVXA-H.]+\s+", "", g[0][1]).title()[:40], "role": "method" if i == 1 else "other", "raw_section_ids": [r for r, _ in g]}
            for i, g in enumerate(groups)
        ]
        sents = _sentences(_section_text(context))
        return {
            "title": (title.group(1) if title else "Untitled paper"),
            "authors": ["A. Author", "B. Author"],
            "method_name": "",
            "one_line": sents[0] if sents else "A paper.",
            "problem": sents[1] if len(sents) > 1 else "A problem.",
            "contribution": sents[2] if len(sents) > 2 else "A contribution.",
            "key_result": "",
            "sections": sections,
        }

    def _task_analyze(self, prompt: str, context: str):
        text = _section_text(context)
        sents = _sentences(text) or [text[:200] or "This section has little text."]
        nums = _numbers_with_labels(text)
        caps = sorted({w for w in re.findall(r"\b[A-Z][A-Za-z0-9\-]{3,}\b", text)})[:6]
        return {
            "summary": " ".join(sents[:2]),
            "key_points": sents[:4],
            "key_quotes": sents[:2],
            "equations": [{"latex": "y = W x + b", "meaning": "A linear map."}] if "=" in text else [],
            "results": [{"claim": f"{lab} reaches {val}.", "metric": "score", "value": str(val), "baseline": ""} for lab, val in nums[:4]],
            "concepts": [{"name": c, "explanation": f"{c} as used in this section."} for c in caps[:3]],
            "prior_work": [{"name": c, "relation": "compared with"} for c in caps[3:6]],
        }

    def _task_profile(self, prompt: str, context: str):
        text = _section_text(context)
        name = re.search(r"Name:\**\s*([A-Z][a-z]+)", text)
        return {
            "name": name.group(1) if name else "Reader",
            "summary": "A curious reader.",
            "expertise_level": "intermediate",
            "depth": "balanced",
            "language": "English",
            "language_code": "en-US",
        }

    def _task_explain(self, prompt: str, context: str):
        lo = int(re.search(r"WRITE (\d+)-(\d+) BEATS", prompt).group(1))
        points = re.findall(r"^- (.+)$", prompt.split("Key points:", 1)[1].split("\n\n", 1)[0], re.M) if "Key points:" in prompt else []
        points = [p for p in points if p != "(none)"] or ["This part of the paper sets up what follows."]
        has_quotes = "[1] " in prompt
        pos = int(re.search(r"section (\d+) of", prompt).group(1))
        deep = "DEEP-DIVE" in prompt
        beats = []
        for i in range(max(lo, min(len(points), lo + 1))):
            p = points[i % len(points)]
            beats.append({"speaker": "narrator", "narration": p, "subtitle": " ".join(p.split()[:8]), "quote": 1 if (i == 0 and has_quotes) else None})
        n_results = len(re.findall(r"\[score: ", prompt))
        if n_results >= 2:
            dtype = "bar"
        elif deep and "Equations:" in prompt:
            dtype = "equation"
        elif pos % 3 == 0:
            dtype = "table"
        else:
            dtype = "flow"
        return {"beats": beats, "diagram": {"needed": True, "type": dtype, "brief": "The core idea of this section."}}

    def _task_diagram(self, prompt: str, context: str):
        dtype = re.search(r"Design one (\w+) diagram", prompt).group(1)
        beat_ids = re.findall(r"^((?:high|deep)\.s\d+\.b\d+):", prompt, re.M)
        text = _section_text(context)
        if dtype == "bar":
            nums = _numbers_with_labels(text, 5)
            if len(nums) >= 2:
                cats = [lab for lab, _ in nums]
                return {
                    "diagram": {"type": "bar", "title": "Reported numbers", "caption": "Values as reported in the section.", "categories": cats,
                                "series": [{"name": "score", "values": [v for _, v in nums]}], "unit": "", "higher_is_better": True},
                    "focus": {b: [cats[i % len(cats)]] for i, b in enumerate(beat_ids)},
                }
            dtype = "flow"
        if dtype == "table":
            rows = [["Input", "what goes in"], ["Process", "what happens"], ["Output", "what comes out"]]
            return {
                "diagram": {"type": "table", "title": "At a glance", "caption": "The section in three rows.", "columns": ["Step", "Role"], "rows": rows},
                "focus": {b: [rows[i % 3][0]] for i, b in enumerate(beat_ids)},
            }
        if dtype == "equation":
            return {
                "diagram": {"type": "equation", "title": "The core equation", "caption": "Each term explained.", "latex": "y = W x + b",
                            "terms": [{"symbol": "W", "meaning": "weights"}, {"symbol": "x", "meaning": "input"}, {"symbol": "b", "meaning": "bias"}]},
                "focus": {b: [["W", "x", "b"][i % 3]] for i, b in enumerate(beat_ids)},
            }
        nodes = [
            {"id": "input", "label": "Input", "detail": "What the method receives.", "kind": "input"},
            {"id": "encode", "label": "Encode", "detail": "Build a representation.", "kind": "process"},
            {"id": "model", "label": "Model", "detail": "The core component.", "kind": "model", "group": "core"},
            {"id": "combine", "label": "Combine", "detail": "Merge the signals.", "kind": "process", "group": "core"},
            {"id": "output", "label": "Output", "detail": "The result.", "kind": "output"},
        ]
        edges = [{"source": a, "target": b, "label": ""} for a, b in [("input", "encode"), ("encode", "model"), ("model", "combine"), ("combine", "output")]]
        return {
            "diagram": {"type": "flow", "title": "How it works", "caption": "From input to output.", "direction": "LR", "nodes": nodes, "edges": edges,
                        "groups": [{"id": "core", "label": "Core"}]},
            "focus": {b: [nodes[(i + 1) % len(nodes)]["id"]] for i, b in enumerate(beat_ids)},
        }

    def _task_graph(self, prompt: str, context: str):
        center = re.search(r'is called "(.*?)"', prompt).group(1)
        text = _section_text(context)
        counts: dict[str, int] = {}
        for w in re.findall(r"\b[A-Z][A-Za-z0-9\-]{2,}\b", text):
            counts[w] = counts.get(w, 0) + 1
        names = [w for w, c in sorted(counts.items(), key=lambda x: (-x[1], x[0])) if c >= 2][:6]
        ents = [{"name": n, "type": "concept", "description": f"{n} as mentioned in the text.", "evidence": n} for n in names]
        rels = [{"source": center, "target": n, "relation": "uses", "description": "mentioned together"} for n in names[:4]]
        return {"entities": ents, "relations": rels}

    def _task_review(self, prompt: str, context: str):
        return {"issues": [], "beats": []}

    def _task_quiz(self, prompt: str, context: str):
        n = int(re.search(r"Write (\d+) multiple-choice", prompt).group(1))
        asked = set(re.findall(r"^- (.+)$", prompt.split("Already asked", 1)[1], re.M)) if "Already asked" in prompt else set()
        wrong = [
            "The paper reports no experiments for this part.",
            "It relies only on hand-written rules.",
            "The authors say the approach failed everywhere.",
            "It is presented as unrelated to prior work.",
        ]
        questions = []
        for i, s in enumerate(_sentences(_section_text(context))):
            subject = " ".join(w.strip(".,;:") for w in s.split()[2:6])
            question = f"What does the paper say about {subject}?"
            if question in asked:
                continue
            questions.append({
                "question": question, "correct": " ".join(s.split()[:18]), "distractors": wrong[i % 2 : i % 2 + 3],
                "explanation": "The section states it directly.", "quote": s, "kind": "concept", "difficulty": "easy",
            })
            if len(questions) >= n:
                break
        return {"questions": questions}

    def _task_quiz_check(self, prompt: str, context: str):
        text = " ".join(_section_text(context).split()).lower()
        answers = []
        for block in re.split(r"\n\s*\n", prompt.split("\n\n", 1)[1]):
            options = re.findall(r"^\s+([A-D]): (.+)$", block, re.M)
            if options:
                hits = [letter for letter, opt in options if " ".join(opt.split()).lower() in text]
                answers.append(hits[0] if len(hits) == 1 else "none")
        return {"answers": answers}

    def _task_quiz_select(self, prompt: str, context: str):
        k = int(re.search(r"Pick exactly (\d+)", prompt).group(1))
        by_section: dict[str, list[str]] = {}
        for cid, section in re.findall(r"^(c\d+) \[(.+?)\] ", prompt, re.M):
            by_section.setdefault(section, []).append(cid)
        keep, depth = [], 0  # round-robin over sections
        while len(keep) < k and any(len(ids) > depth for ids in by_section.values()):
            keep += [ids[depth] for ids in by_section.values() if len(ids) > depth][: k - len(keep)]
            depth += 1
        return {"keep": keep}

    def _task_questions(self, prompt: str, context: str):
        return {"questions": [
            "What problem does this paper solve?",
            "How does the method work, step by step?",
            "What are the main results?",
            "What are the limitations?",
            "How does it compare with prior work?",
        ]}

    def _task_answer(self, prompt: str, context: str):
        m = re.search(r"\[(s\d+)\] \((.*?)\)\n(.+)", context)
        if not m:
            return "The paper does not say."
        sents = _sentences(m.group(3)) or [m.group(3)[:200]]
        return f"{sents[0]} [{m.group(1)}]"

    def _task_chat(self, prompt: str, context: str):
        return "ok"
