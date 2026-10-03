"""Stage 1 - parse: PDF (local file, PDF URL or arXiv link) -> ParsedPaper.

Layout heuristics on PyMuPDF spans: body font size, bold/larger headings with
numbering or well-known names, repeated header/footer removal, reference
splitting. Falls back to page chunks when no heading structure is found.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..models import Figure, ParsedPaper, RawSection
from .base import RunContext, Stage

KNOWN_HEADINGS = {
    "abstract", "introduction", "related work", "related works", "background", "preliminaries",
    "method", "methods", "methodology", "approach", "our approach", "model", "model architecture",
    "proposed method", "experiments", "experiment", "experimental setup", "experimental results",
    "evaluation", "results", "discussion", "analysis", "conclusion", "conclusions",
    "conclusion and future work", "limitations", "future work", "references", "bibliography",
    "acknowledgments", "acknowledgements", "acknowledgment", "acknowledgement", "appendix",
    "broader impact", "broader impacts", "ethics statement", "supplementary material",
}
# Unnumbered headings still trusted in a numbered document.
UNNUMBERED_OK = {
    "abstract", "references", "bibliography", "acknowledgments", "acknowledgements", "acknowledgment",
    "acknowledgement", "appendix", "supplementary material", "broader impact", "broader impacts",
    "ethics statement", "limitations",
}
DROP_HEADINGS = {"acknowledgments", "acknowledgements", "acknowledgment", "acknowledgement"}
REF_HEADINGS = {"references", "bibliography", "reference"}

_NUMBERED = re.compile(
    r"^(?P<num>\d{1,2}(?:\.\d{1,2}){0,3}|[IVX]{1,5}|[A-H](?:\.\d{1,2}){0,2})(?:\.|\s)\s*(?P<title>[A-Za-z].{0,110})$"
)
_NUM_ONLY = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3}|[IVX]{1,5}|[A-H])\.?$")
_CAPTION = re.compile(r"^(fig(?:ure)?|table|algorithm|listing)\.?\s*(\d+|[ivx]+)\b", re.I)
_ARXIV_ID = re.compile(r"(\d{4}\.\d{4,5}(?:v\d+)?|[a-z\-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)")


@dataclass
class Line:
    page: int
    block: int
    text: str
    size: float
    bold: bool
    y0: float
    y1: float
    x0: float


# --------------------------------------------------------------- fetching


def resolve_source(source: str, ctx: RunContext | None = None) -> Path:
    """Return a local PDF path for a file path, PDF URL, arXiv URL or arXiv id."""
    p = Path(source).expanduser()
    if p.is_file():
        return p
    url = source.strip()
    if url.lower().startswith("arxiv:"):
        url = "https://arxiv.org/abs/" + url.split(":", 1)[1]
    elif re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?", url):
        url = "https://arxiv.org/abs/" + url
    if not re.match(r"^https?://", url):
        raise FileNotFoundError(f"paper not found: {source!r} (expected a PDF path, a PDF URL or an arXiv link)")
    if "arxiv.org/abs/" in url or "arxiv.org/pdf/" in url:
        m = _ARXIV_ID.search(url.split("arxiv.org/", 1)[1])
        if m:
            url = f"https://arxiv.org/pdf/{m.group(1)}"
    key = hashlib.sha256(url.encode()).hexdigest()[:24]
    if ctx is not None:
        cached = ctx.cache.blob_path("downloads", key, "pdf")
        if cached.is_file():
            return cached
    try:
        r = httpx.get(url, follow_redirects=True, timeout=60, headers={"User-Agent": "papermap/0.1"})
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise RuntimeError(f"could not download {url}: {e}") from e
    if not r.content.startswith(b"%PDF"):
        raise RuntimeError(
            f"{url} did not return a PDF (content-type {r.headers.get('content-type')}). "
            "Only PDF files, PDF URLs and arXiv links are supported."
        )
    if ctx is None:
        import tempfile

        out = Path(tempfile.gettempdir()) / f"papermap-{key}.pdf"
        out.write_bytes(r.content)
        return out
    return ctx.cache.put_blob("downloads", key, "pdf", r.content)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- parsing


def _extract_lines(doc) -> tuple[list[Line], list[float]]:
    import pymupdf

    flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
    lines: list[Line] = []
    heights: list[float] = []
    for pno, page in enumerate(doc):
        heights.append(page.rect.height)
        d = page.get_text("dict", flags=flags | pymupdf.TEXT_DEHYPHENATE)
        for bno, block in enumerate(d.get("blocks", [])):
            if block.get("type", 0) != 0:
                continue
            for line in block.get("lines", []):
                dx, dy = line.get("dir", (1, 0))
                if abs(dy) > 0.1 or dx < 0.9:  # rotated text (arXiv stamp, axis labels)
                    continue
                spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
                if not spans:
                    continue
                text = "".join(s["text"] for s in line["spans"]).strip()
                text = re.sub(r"\s+", " ", text)
                # U+FFFD = glyph without a unicode mapping; between letters it is almost always an apostrophe
                text = re.sub("(?<=[A-Za-z])�(?=[A-Za-z])", "'", text).replace("�", "")
                total = sum(len(s["text"].strip()) for s in spans) or 1
                size = sum(s["size"] * len(s["text"].strip()) for s in spans) / total
                bold_chars = sum(
                    len(s["text"].strip())
                    for s in spans
                    if (s.get("flags", 0) & 16) or re.search(r"bold|black|heavy|semibold|medi", s.get("font", ""), re.I)
                )
                x0, y0, _, y1 = line["bbox"]
                lines.append(Line(pno, bno, text, round(size, 1), bold_chars / total > 0.6, y0, y1, x0))
    return lines, heights


def _strip_headers_footers(lines: list[Line], heights: list[float]) -> list[Line]:
    n_pages = len(heights)
    margin_keys: Counter[str] = Counter()
    keyed: list[tuple[Line, str | None]] = []
    for ln in lines:
        h = heights[ln.page]
        in_margin = ln.y1 < 0.075 * h or ln.y0 > 0.925 * h
        key = re.sub(r"\d+", "#", ln.text.lower()) if in_margin else None
        keyed.append((ln, key))
        if key:
            margin_keys[(key)] += 1
    threshold = max(2, int(0.3 * n_pages))
    out = []
    for ln, key in keyed:
        if key is not None:
            if margin_keys[key] >= threshold or re.fullmatch(r"[#\s\-–/of]*", key):
                continue
        out.append(ln)
    return out


def _body_size(lines: list[Line]) -> float:
    c: Counter[float] = Counter()
    for ln in lines:
        c[round(ln.size * 2) / 2] += len(ln.text)
    return c.most_common(1)[0][0] if c else 10.0


def _clean_heading(text: str) -> str:
    return re.sub(r"[\s:.]+$", "", text).strip()


def _heading_info(ln: Line, body: float, alone: bool = False) -> tuple[str, int] | None:
    text = ln.text.strip()
    if not (2 <= len(text) <= 120) or len(text.split()) > 14:
        return None
    if _CAPTION.match(text) or not re.search(r"[A-Za-z]{2}", text):
        return None
    if text.endswith(",") or text.endswith(";"):
        return None
    bigger = ln.size >= body + 0.9
    styled = bigger or (ln.bold and ln.size >= body - 0.6)
    if not styled:
        clean = _clean_heading(text).lower()
        if clean in KNOWN_HEADINGS and (text.isupper() or alone):
            return _clean_heading(text), 1
        return None
    m = _NUMBERED.match(text)
    if m:
        num, title = m.group("num"), _clean_heading(m.group("title"))
        if len(title.split()) > 12 or title.endswith(".") and len(title.split()) > 8:
            return None
        if re.fullmatch(r"[A-H]", num) and not (ln.bold or bigger):
            return None
        if title[:1].islower():
            return None
        level = num.count(".") + 1 if num[0].isdigit() else 1
        return f"{num} {title}", level
    clean = _clean_heading(text).lower()
    if clean in KNOWN_HEADINGS:
        return _clean_heading(text), 1
    if ln.size >= body + 1.8 and len(text.split()) <= 10 and text[:1].isupper() and not text.endswith("."):
        return _clean_heading(text), 1
    return None


def _title_from_first_page(lines: list[Line], heights: list[float]) -> str:
    first = [ln for ln in lines if ln.page == 0 and ln.y0 < 0.5 * heights[0] and len(ln.text) > 3]
    if not first:
        return ""
    top = max(ln.size for ln in first)
    title = ""
    started = False
    for ln in first:
        if ln.size >= top - 1.5 and ln.size > 11 or abs(ln.size - top) < 0.6:
            if title.endswith("-"):
                title = title[:-1] + ln.text
            else:
                title = f"{title} {ln.text}"
            started = True
        elif started:
            break
    return re.sub(r"\s+", " ", title).strip()


def _sane_numbering(heads: list[tuple[int, tuple[str, int]]]) -> list[tuple[int, tuple[str, int]]]:
    """Drop "headings" whose numbering breaks the document's sequence (table
    cells like "73.7 MultiNLI"), and letter headings before the references."""
    out = []
    top = 0
    seen_refs = False
    for i, (title, level) in heads:
        m = re.match(r"^(\d{1,2})((?:\.\d{1,2})*)\s", title)
        name = re.sub(r"^[\dIVXA-H.]+\s+", "", title).strip().lower()
        if m:
            n = int(m.group(1))
            if n == 0 or n > top + 1 and not (top == 0 and n <= 2) or n < top:
                continue
            if m.group(2) and n != top:
                continue
            top = n
        elif re.match(r"^[A-H](\.\d{1,2})*\s", title) and name not in KNOWN_HEADINGS:
            if not seen_refs:
                continue
        if name in REF_HEADINGS or title.lower() in REF_HEADINGS:
            seen_refs = True
        out.append((i, (title, level)))
    return out


def _paragraphs(lines: list[Line]) -> str:
    paras: list[str] = []
    cur: list[str] = []
    last_key = None
    for ln in lines:
        key = (ln.page, ln.block)
        if last_key is not None and key != last_key and cur:
            paras.append(" ".join(cur))
            cur = []
        cur.append(ln.text)
        last_key = key
    if cur:
        paras.append(" ".join(cur))
    text = "\n\n".join(p.strip() for p in paras if p.strip())
    return re.sub(r"(\w)- (\w)", lambda m: m.group(1) + m.group(2) if m.group(2).islower() else m.group(0), text)


def _split_references(text: str) -> list[str]:
    if len(re.findall(r"\[\d{1,3}\]\s", text)) >= 5:  # numbered style: markers delimit entries
        parts = re.split(r"(?=\[\d{1,3}\]\s)", re.sub(r"\s+", " ", text))
    else:  # author-year style: one PDF block per entry
        parts = text.split("\n\n")
    refs = [re.sub(r"\s+", " ", p).strip() for p in parts]
    return [r for r in refs if len(r) > 25][:400]


def parse_pdf(path: Path, source: str | None = None) -> ParsedPaper:
    import pymupdf

    doc = pymupdf.open(path)
    try:
        lines, heights = _extract_lines(doc)
        meta_title = (doc.metadata or {}).get("title") or ""
        n_pages = len(doc)
    finally:
        doc.close()
    if not lines:
        raise RuntimeError(f"{path} contains no extractable text (scanned PDF? OCR it first)")
    lines = _strip_headers_footers(lines, heights)
    body = _body_size(lines)
    title = _title_from_first_page(lines, heights)
    if len(title) < 8 and len(meta_title) > 8 and not re.search(r"\.(pdf|dvi|tex|docx?)$|untitled|microsoft", meta_title, re.I):
        title = meta_title
    first_page_text = _paragraphs([ln for ln in lines if ln.page == 0])[:6000]

    block_sizes = Counter((ln.page, ln.block) for ln in lines)

    # 1) find heading lines (merging "3" + "Method" split across lines)
    heads: dict[int, tuple[str, int]] = {}
    i = 0
    while i < len(lines):
        ln = lines[i]
        if _NUM_ONLY.match(ln.text) and i + 1 < len(lines):
            nxt = lines[i + 1]
            merged = Line(ln.page, nxt.block, f"{ln.text} {nxt.text}", max(ln.size, nxt.size), ln.bold or nxt.bold, ln.y0, nxt.y1, ln.x0)
            info = _heading_info(merged, body)
            if info and nxt.page == ln.page and abs(nxt.y0 - ln.y0) < 3 * max(ln.size, 1):
                heads[i] = info
                heads[i + 1] = ("", -1)  # consumed
                i += 2
                continue
        info = _heading_info(ln, body, alone=block_sizes[(ln.page, ln.block)] == 1)
        if info:
            heads[i] = info
        i += 1

    real_heads = _sane_numbering([(i, h) for i, h in sorted(heads.items()) if h[1] != -1])

    # Sanity: if numbered headings exist, discard unnumbered "big text" guesses that
    # are not well-known names (usually figure text or pull quotes).
    has_numbered = sum(1 for _, (t, _) in real_heads if re.match(r"^(\d|[IVX]+\s)", t)) >= 3
    if has_numbered:
        real_heads = [
            (i, (t, lv)) for i, (t, lv) in real_heads
            if re.match(r"^(\d{1,2}(\.\d{1,2})*|[IVX]{1,5}|[A-H](\.\d{1,2})*)\s", t) or _clean_heading(t).lower() in UNNUMBERED_OK
        ]

    sections: list[RawSection] = []
    references: list[str] = []
    figures: list[Figure] = []

    def add_section(heading: str, level: int, chunk: list[Line], page: int = 0) -> None:
        text = _paragraphs(chunk)
        key = re.sub(r"^[\dIVXA-H.]+\s+", "", heading).strip().lower()
        if key in REF_HEADINGS:
            references.extend(_split_references(text))
            return
        if key in DROP_HEADINGS:
            return
        pages = [ln.page + 1 for ln in chunk] or [page]
        sections.append(
            RawSection(
                id=f"r{len(sections) + 1}",
                heading=heading,
                level=level,
                text=text,
                page_start=min(pages),
                page_end=max(pages),
            )
        )

    if len(real_heads) >= 3:
        head_idx = [i for i, _ in real_heads]
        # front matter: keep only from an "Abstract" line onward
        front = [ln for j, ln in enumerate(lines[: head_idx[0]]) if heads.get(j, ("", 0))[1] != -1]
        abs_start = next((k for k, ln in enumerate(front) if ln.text.lower().startswith("abstract")), None)
        if abs_start is not None:
            chunk = front[abs_start:]
            if chunk and chunk[0].text.lower().strip(" .:—-") == "abstract":
                chunk = chunk[1:]
            elif chunk:
                first = re.sub(r"^abstract[\s.:—\-–]*", "", chunk[0].text, flags=re.I)
                chunk = [Line(chunk[0].page, chunk[0].block, first, chunk[0].size, chunk[0].bold, chunk[0].y0, chunk[0].y1, chunk[0].x0)] + chunk[1:]
            if chunk:
                add_section("Abstract", 1, chunk)
        for n, (i, (heading, level)) in enumerate(real_heads):
            end = head_idx[n + 1] if n + 1 < len(real_heads) else len(lines)
            chunk = [ln for j, ln in enumerate(lines[i + 1 : end], start=i + 1) if heads.get(j, ("", 0))[1] != -1]
            add_section(heading, level, chunk, page=lines[i].page + 1)
    else:
        # No usable heading structure: chunk by pages.
        per = 2
        for start in range(0, n_pages, per):
            chunk = [ln for ln in lines if start <= ln.page < start + per]
            if chunk:
                add_section(f"Pages {start + 1}-{min(n_pages, start + per)}", 1, chunk)

    for ln_i, ln in enumerate(lines):
        m = _CAPTION.match(ln.text)
        if m and m.group(1).lower().startswith("fig") and (":" in ln.text[:14] or "." in ln.text[:12]):
            block_text = " ".join(x.text for x in lines if x.page == ln.page and x.block == ln.block)
            fid = f"fig{m.group(2)}"
            if all(f.id != fid for f in figures):
                figures.append(Figure(id=fid, caption=block_text[:600], page=ln.page + 1))

    return ParsedPaper(
        source=source or str(path),
        sha256=file_sha256(path),
        title=title or path.stem,
        first_page_text=first_page_text,
        n_pages=n_pages,
        sections=[s for s in sections if s.text.strip() or s.level == 1],
        references=references,
        figures=figures,
    )


def _run(ctx: RunContext, deps: dict) -> ParsedPaper:
    log = STAGE.log()
    path = resolve_source(ctx.source, ctx)
    paper = parse_pdf(path, source=ctx.source)
    chars = sum(len(s.text) for s in paper.sections)
    log.info(
        "%s: %d pages, %d sections, %d references, %d figures, %s chars",
        paper.title[:70], paper.n_pages, len(paper.sections), len(paper.references), len(paper.figures), f"{chars:,}",
    )
    for s in paper.sections:
        log.debug("  %s [%s] p%d-%d %d chars", s.id, s.heading, s.page_start, s.page_end, len(s.text))
    return paper


def _key(ctx: RunContext):
    p = Path(ctx.source).expanduser()
    return file_sha256(p) if p.is_file() else ctx.source


STAGE = Stage(
    name="parse",
    version="1",
    deps=(),
    output=ParsedPaper,
    run=_run,
    uses_llm=False,
    key_extra=_key,
    description="PDF/arXiv -> text sections, references, figure captions",
)
