"""Helpers shared by the LLM stages."""

from __future__ import annotations

import re

from ..models import OVERVIEW_ID, ParsedPaper, SectionUnderstanding, Understanding


def split_text(text: str, max_chars: int) -> list[str]:
    """Deterministically split text into <= max_chars parts at paragraph boundaries."""
    if len(text) <= max_chars:
        return [text]
    parts: list[str] = []
    cur = ""
    for para in text.split("\n\n"):
        while len(para) > max_chars:  # a single giant paragraph: cut at sentence ends
            cut = para.rfind(". ", 0, max_chars)
            cut = cut + 1 if cut > max_chars // 2 else max_chars
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(para[:cut].strip())
            para = para[cut:].strip()
        if cur and len(cur) + len(para) + 2 > max_chars:
            parts.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        parts.append(cur)
    return parts


def raw_text(paper: ParsedPaper, raw_id: str, max_chars: int) -> str:
    """Text for a raw section id, or for one part of it ("r7#2")."""
    base, _, part = raw_id.partition("#")
    sec = paper.section(base)
    if sec is None:
        return ""
    if not part:
        return sec.text
    parts = split_text(sec.text, max_chars)
    idx = int(part) - 1
    return parts[idx] if 0 <= idx < len(parts) else ""


def section_text(paper: ParsedPaper, su: SectionUnderstanding, max_chars: int) -> str:
    chunks = []
    for rid in su.raw_section_ids:
        base = rid.partition("#")[0]
        sec = paper.section(base)
        if sec is None:
            continue
        body = raw_text(paper, rid, max_chars)
        label = sec.heading + (f" (part {rid.partition('#')[2]})" if "#" in rid else "")
        chunks.append(f"## {label}\n{body}")
    return "\n\n".join(chunks)


def overview_text(paper: ParsedPaper, und: Understanding, max_chars: int) -> str:
    """Grounding text for the synthetic overview section: abstract + section summaries."""
    s0 = und.section(OVERVIEW_ID)
    abstract = section_text(paper, s0, max_chars) if s0 and s0.raw_section_ids else paper.first_page_text
    lines = [f"- {s.title}: {s.summary}" for s in und.sections if s.id != OVERVIEW_ID]
    return f"{abstract}\n\n## Section summaries\n" + "\n".join(lines)


def grounding_text(paper: ParsedPaper, und: Understanding, section_id: str, max_chars: int) -> str:
    su = und.section(section_id)
    if su is None:
        return ""
    if section_id == OVERVIEW_ID:
        return overview_text(paper, und, max_chars)
    return section_text(paper, su, max_chars)


def context_block(paper_title: str, label: str, text: str) -> str:
    return f'PAPER: "{paper_title}"\n\n{label}:\n<<<\n{text.strip()}\n>>>'


_NUM = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)*)(?:\s*%)?")


def numbers_in(text: str) -> list[str]:
    """Numbers worth grounding: anything with a decimal point or >= 2 digits."""
    out = []
    for m in _NUM.finditer(text):
        n = m.group(1).rstrip(".,")
        digits = re.sub(r"\D", "", n)
        if "." in n or len(digits) >= 2:
            if re.fullmatch(r"(19|20)\d\d", n):  # years
                continue
            out.append(n)
    return out
