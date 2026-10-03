"""Enrich a PDF parse with the arXiv LaTeX source, structured tables and the
paper's own figures (rendered to PNG in the cache)."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ..log import get_logger
from ..models import Figure, ParsedPaper, RawSection, Table
from . import arxiv
from .latex import LatexPaper, parse_latex, table_markdown

log = get_logger("enrich")

MAX_FIGURE_PX = 1400


def _norm(t: str) -> str:
    t = t.replace("ﬁ", "fi").replace("ﬂ", "fl")
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def _page_texts(doc) -> list[str]:
    return [_norm(p.get_text()) for p in doc]


def _find_page(texts: list[str], phrase: str, start: int = 0, words: int = 5) -> int | None:
    # citations and math render differently in the PDF ("[Liu et al., 2024]" vs "[33]"): match the words before them
    plain = re.split(r"\[|\$", phrase, maxsplit=1)[0]
    if len(plain.split()) < 3:
        plain = re.sub(r"\[[^\]]*\]|\$[^$]*\$", " ", phrase)
    key = " ".join(_norm(plain).split()[:words])
    if len(key) < 6:
        return None
    for i in list(range(start, len(texts))) + list(range(0, start)):
        if key in texts[i]:
            return i + 1
    return None


def render_image_file(path: Path, cache) -> str | None:
    """PDF/PNG/JPG figure file -> PNG in the cache; returns its cache-relative path."""
    import pymupdf

    try:
        doc = pymupdf.open(path)
        page = doc[0]
        zoom = max(0.2, min(4.0, MAX_FIGURE_PX / max(page.rect.width, 1)))
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        data = pix.tobytes("png")
        doc.close()
    except Exception as e:  # unsupported/corrupt figure: skip it, never fail the parse
        log.debug("could not render figure %s: %s", path.name, e)
        return None
    key = hashlib.sha256(data).hexdigest()[:20]
    cache.put_blob("figures", key, "png", data)
    return f"figures/{key}.png"


def find_latex(paper: ParsedPaper, source: str, cache) -> tuple[str, Path] | None:
    """(arxiv id, source dir) for this paper, if it is on arXiv with LaTeX source."""
    arxiv_id = arxiv.id_from_source(source) or arxiv.id_from_text(paper.first_page_text)
    if not arxiv_id:
        arxiv_id = arxiv.search_by_title(paper.title, paper.first_page_text)
        if arxiv_id:
            log.info("found the paper on arXiv by title: %s", arxiv_id)
    if not arxiv_id:
        return None
    src = arxiv.fetch_source(arxiv_id, cache)
    return (arxiv_id, src) if src else None


def merge_latex(pdf: ParsedPaper, lp: LatexPaper, cache, arxiv_id: str) -> ParsedPaper:
    import pymupdf

    texts: list[str] = []
    if pdf.pdf_path:
        with pymupdf.open(pdf.pdf_path) as doc:
            texts = _page_texts(doc)
    n_pages = pdf.n_pages

    sections: list[RawSection] = []
    if lp.abstract:
        sections.append(RawSection(id="r1", heading="Abstract", level=1, text=lp.abstract, page_start=1, page_end=1))
    starts: list[int] = []
    cur = 0
    for s in lp.sections:
        heading = re.sub(r"^[A-Z0-9.]+\s+", "", s.heading)
        page = _find_page(texts, heading, start=max(0, cur - 1), words=6) if texts else None
        if page and page >= cur:
            cur = page
        starts.append(cur or 1)
    rid_of: list[str] = []
    for k, s in enumerate(lp.sections):
        start = starts[k]
        end = max(start, starts[k + 1] if k + 1 < len(starts) else n_pages)
        rid = f"r{len(sections) + 1}"
        rid_of.append(rid)
        sections.append(RawSection(id=rid, heading=s.heading, level=s.level, text=s.text, page_start=start, page_end=end))

    def raw_id(index: int) -> str | None:
        return rid_of[index] if 0 <= index < len(rid_of) else None

    figures = []
    for f in lp.figures:
        files = [k for k in (render_image_file(p, cache) for p in f.files) if k]
        figures.append(Figure(
            id=f"fig{f.number}", number=f.number, caption=f.caption, files=files,
            page=(_find_page(texts, f.caption) or 0) if texts else 0,
            raw_section_id=raw_id(f.section_index), sub_captions=f.sub_captions,
        ))
    tables = [
        Table(id=f"tab{t.number}", number=t.number, caption=t.caption, columns=t.columns, rows=t.rows,
              page=_find_page(texts, t.caption) if texts else None, raw_section_id=raw_id(t.section_index))
        for t in lp.tables
    ]
    return pdf.model_copy(update={
        "title": lp.title or pdf.title,
        "sections": sections,
        "references": lp.references or pdf.references,
        "figures": figures,
        "tables": tables,
        "source_kind": "latex",
        "arxiv_id": arxiv_id,
    })


# ------------------------------------------------------------ PDF fallbacks


def _section_for_page(paper: ParsedPaper, page: int) -> RawSection | None:
    hits = [s for s in paper.sections if s.page_start <= page <= s.page_end and s.heading != "Abstract"]
    return hits[-1] if hits else None


def pdf_tables(paper: ParsedPaper) -> ParsedPaper:
    """Detect ruled tables in the PDF (only those with a "Table N" caption nearby)."""
    import pymupdf

    if not paper.pdf_path:
        return paper
    tables: list[Table] = []
    sections = {s.id: s.model_copy() for s in paper.sections}
    with pymupdf.open(paper.pdf_path) as doc:
        for pno, page in enumerate(doc):
            try:
                found = page.find_tables().tables
            except Exception:  # noqa: BLE001 - table detection is best effort
                continue
            for t in found:
                rows = [[re.sub(r"\s+", " ", (c or "")).strip() for c in row] for row in t.extract()]
                rows = [r for r in rows if any(r)]
                if len(rows) < 2 or max(len(r) for r in rows) < 2:
                    continue
                if sum(bool(re.search(r"\d", c)) for r in rows for c in r) < 2:
                    continue
                x0, y0, x1, y1 = t.bbox
                near = page.get_text("text", clip=pymupdf.Rect(0, max(0, y0 - 70), page.rect.width, min(page.rect.height, y1 + 70)))
                m = re.search(r"Table\s+(\d+)[.:]\s*([^\n]+(?:\n[^\n]+)?)", near)
                if not m:
                    continue
                number = int(m.group(1))
                if any(tb.number == number for tb in tables):
                    continue
                sec = _section_for_page(paper, pno + 1)
                width = max(len(r) for r in rows)
                rows = [r + [""] * (width - len(r)) for r in rows]
                table = Table(id=f"tab{number}", number=number, caption=" ".join(m.group(2).split()), columns=rows[0],
                              rows=rows[1:], page=pno + 1, raw_section_id=sec.id if sec else None)
                tables.append(table)
                if sec:
                    s = sections[sec.id]
                    s.text = f"{s.text}\n\n{table_markdown(number, table.caption, rows)}"
    if not tables:
        return paper
    log.info("extracted %d tables from the PDF", len(tables))
    return paper.model_copy(update={"tables": tables, "sections": [sections[s.id] for s in paper.sections]})


def pdf_figures(paper: ParsedPaper, cache) -> ParsedPaper:
    """Crop each captioned figure from its PDF page (images + vector drawings above the caption)."""
    import pymupdf

    if not paper.pdf_path or not paper.figures:
        return paper
    out = []
    with pymupdf.open(paper.pdf_path) as doc:
        for f in paper.figures:
            if not f.page or f.page > len(doc):
                out.append(f)
                continue
            page = doc[f.page - 1]
            words = " ".join(f.caption.split()[:6])
            hits = page.search_for(words) if words else []
            if not hits:
                out.append(f)
                continue
            cap = hits[0]
            ph = page.rect.height
            boxes = [pymupdf.Rect(i["bbox"]) for i in page.get_image_info()]
            try:
                boxes += [d["rect"] for d in page.get_drawings() if d.get("rect")]
            except Exception:  # noqa: BLE001
                pass
            region = None
            for b in boxes:
                if b.y1 <= cap.y0 + 2 and b.y0 >= cap.y0 - 0.85 * ph and b.width > 8 and b.height > 8:
                    region = b if region is None else region | b
            if region is None or region.height < 40 or region.width < 60:
                out.append(f)
                continue
            region = (region + (-6, -6, 6, 6)) & page.rect
            zoom = max(1.0, min(3.0, MAX_FIGURE_PX / max(region.width, 1)))
            data = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=region, alpha=False).tobytes("png")
            key = hashlib.sha256(data).hexdigest()[:20]
            cache.put_blob("figures", key, "png", data)
            num = re.search(r"\d+", f.id)
            sec = _section_for_page(paper, f.page)
            out.append(f.model_copy(update={"files": [f"figures/{key}.png"], "number": int(num.group(0)) if num else 0,
                                            "raw_section_id": sec.id if sec else None}))
    return paper.model_copy(update={"figures": out})


def enrich(paper: ParsedPaper, source: str, cache, use_latex: bool = True, figures: bool = True) -> ParsedPaper:
    if use_latex:
        try:
            found = find_latex(paper, source, cache)
        except Exception as e:  # noqa: BLE001 - network or archive trouble: keep the PDF parse
            log.warning("LaTeX source lookup failed (%s); using the PDF", e)
            found = None
        if found:
            arxiv_id, src = found
            try:
                lp = parse_latex(src)
            except Exception as e:  # noqa: BLE001
                log.warning("could not parse the LaTeX source of arXiv:%s (%s); using the PDF", arxiv_id, e)
                lp = None
            if lp and len(lp.sections) >= 3:
                merged = merge_latex(paper, lp, cache, arxiv_id)
                log.info("using the LaTeX source of arXiv:%s: %d sections, %d figures, %d tables, exact equations",
                         arxiv_id, len(lp.sections), len(lp.figures), len(lp.tables))
                return merged if figures else merged.model_copy(update={"figures": []})
    paper = pdf_tables(paper)
    return pdf_figures(paper, cache) if figures else paper
