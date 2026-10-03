"""PDF page images and quote locations for the source viewer."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ..log import get_logger
from ..models import PageImage, SourceRef

log = get_logger("pdfview")

PAGE_PX = 1100  # rendered page width


def render_pages(pdf_path: str, out_dir: Path) -> list[PageImage]:
    """Render every page to pages/p<n>.jpg (skipped when already rendered from the same PDF)."""
    import pymupdf

    pages_dir = out_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest()[:16]
    marker = pages_dir / "source.sha"
    fresh = marker.is_file() and marker.read_text().strip() == digest
    images: list[PageImage] = []
    with pymupdf.open(pdf_path) as doc:
        for i, page in enumerate(doc):
            target = pages_dir / f"p{i + 1}.jpg"
            if not (fresh and target.is_file()):
                zoom = PAGE_PX / max(page.rect.width, 1)
                pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
                target.write_bytes(pix.tobytes(output="jpg", jpg_quality=82))
            images.append(PageImage(src=f"pages/p{i + 1}.jpg", width=page.rect.width, height=page.rect.height))
    marker.write_text(digest)
    return images


def _plain_runs(text: str) -> list[str]:
    """Pieces of a quote that look the same in the PDF: no math, citations or equation refs."""
    runs = re.split(r"\$[^$]*\$|\[[^\]]*\]|\(\d+\)|[“”\"]", text)
    out = [" ".join(r.split()) for r in runs]
    return sorted((r for r in out if len(r.split()) >= 4), key=len, reverse=True)


class QuoteLocator:
    def __init__(self, pdf_path: str):
        import pymupdf

        self.doc = pymupdf.open(pdf_path)
        self.cache: dict[str, SourceRef | None] = {}

    def close(self) -> None:
        self.doc.close()

    def locate(self, quote: str, hint_pages: list[int]) -> SourceRef | None:
        if quote in self.cache:
            return self.cache[quote]
        runs = _plain_runs(quote)
        found = None
        if runs:
            n = len(self.doc)
            order = [p for p in hint_pages if 1 <= p <= n] + [p for p in range(1, n + 1) if p not in hint_pages]
            snippets = []
            for r in runs[:3]:
                words = r.split()
                snippets.append(" ".join(words[:9]))
                if len(words) > 14:
                    snippets.append(" ".join(words[-7:]))
            for pno in order:
                page = self.doc[pno - 1]
                rects = page.search_for(runs[0]) if len(runs[0]) <= 400 else []
                if not rects:
                    for snip in snippets:
                        rects += page.search_for(snip)
                if rects:
                    w, h = page.rect.width, page.rect.height
                    boxes = sorted({(round(r.x0 / w, 4), round(r.y0 / h, 4), round(r.x1 / w, 4), round(r.y1 / h, 4)) for r in rects},
                                   key=lambda b: (b[1], b[0]))
                    found = SourceRef(page=pno, rects=[list(b) for b in boxes[:24]])
                    break
        self.cache[quote] = found
        return found
