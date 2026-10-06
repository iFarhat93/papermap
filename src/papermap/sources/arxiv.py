"""Find a paper's arXiv identifier and fetch its LaTeX source (e-print).

Resolution order: arXiv id in the given link -> "arXiv:XXXX.XXXXX" stamp in the
PDF -> exact-title search on the arXiv API (accepted only on a near-identical
title). Downloads go through the shared cache.
"""

from __future__ import annotations

import gzip
import io
import re
import tarfile
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote

import httpx

from ..log import get_logger

log = get_logger("arxiv")

UA = {"User-Agent": "papermap/0.1"}
_ID = r"(\d{4}\.\d{4,5})(v\d+)?"
_ID_IN_LINK = re.compile(r"arxiv\.org/(?:abs|pdf|e-print|html)/" + _ID, re.I)
_ID_STAMP = re.compile(r"arXiv\s*:\s*" + _ID, re.I)
_ID_BARE = re.compile(r"^(?:arxiv:)?" + _ID + r"$", re.I)


def id_from_source(source: str) -> str | None:
    s = source.strip()
    m = _ID_IN_LINK.search(s) or _ID_BARE.match(s)
    return (m.group(1) + (m.group(2) or "")) if m else None


def id_from_text(text: str) -> str | None:
    m = _ID_STAMP.search(text or "")
    return (m.group(1) + (m.group(2) or "")) if m else None


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def titles_match(a: str, b: str, threshold: float = 0.92) -> bool:
    na, nb = _norm_title(a), _norm_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return SequenceMatcher(None, na, nb).ratio() >= threshold


def search_by_title(title: str, first_page_text: str = "", timeout: float = 30) -> str | None:
    """arXiv API title search; only an (almost) identical title is accepted."""
    words = [w for w in _norm_title(title).split() if len(w) > 2][:12]
    if len(words) < 3:
        return None
    query = "+AND+".join(f"ti:{quote(w)}" for w in words)
    url = f"https://export.arxiv.org/api/query?search_query={query}&max_results=5"
    try:
        r = httpx.get(url, headers=UA, timeout=timeout, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError as e:
        log.debug("arXiv search failed: %s", e)
        return None
    page = _norm_title(first_page_text)
    for entry in re.findall(r"<entry>(.*?)</entry>", r.text, re.S):
        t = re.search(r"<title>(.*?)</title>", entry, re.S)
        i = re.search(r"<id>https?://arxiv\.org/abs/" + _ID + r"</id>", entry)
        if not t or not i:
            continue
        cand = " ".join(t.group(1).split())
        # the candidate title must match the paper's title, or appear on its first page
        if titles_match(cand, title) or (page and _norm_title(cand) in page):
            return i.group(1) + (i.group(2) or "")
    return None


def _safe_extract(data: bytes, dest: Path) -> bool:
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
            members = []
            for m in tar.getmembers():
                target = (dest / m.name).resolve()
                if not str(target).startswith(str(dest.resolve())) or m.issym() or m.islnk() or m.isdev():
                    continue  # path traversal / links: skip
                members.append(m)
            try:
                tar.extractall(dest, members=members, filter="data")
            except TypeError:  # Python without extraction filters
                tar.extractall(dest, members=members)
        return True
    except tarfile.TarError:
        pass
    # a single gzipped .tex file (no tar)
    try:
        text = gzip.decompress(data)
    except OSError:
        text = data
    if b"\\documentclass" in text:
        (dest / "main.tex").write_bytes(text)
        return True
    return False


def fetch_source(arxiv_id: str, cache, timeout: float = 120) -> Path | None:
    """Download and unpack the e-print; returns the source directory or None."""
    key = re.sub(r"[^0-9A-Za-z.]", "_", arxiv_id)
    dest = cache.touch(cache.root / "latex" / key)
    if dest.is_dir() and any(dest.rglob("*.tex")):
        return dest
    blob = cache.blob_path("downloads", f"arxiv-src-{key}", "bin")
    if blob.is_file():
        data = blob.read_bytes()
    else:
        url = f"https://arxiv.org/e-print/{arxiv_id}"
        try:
            r = httpx.get(url, headers=UA, timeout=timeout, follow_redirects=True)
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("could not download the LaTeX source of arXiv:%s (%s)", arxiv_id, e)
            return None
        data = r.content
        if data[:5] == b"%PDF-":  # the authors only submitted a PDF
            log.info("arXiv:%s has no LaTeX source (PDF-only submission)", arxiv_id)
            return None
        cache.put_blob("downloads", f"arxiv-src-{key}", "bin", data)
    if not _safe_extract(data, dest):
        log.warning("arXiv:%s source could not be unpacked", arxiv_id)
        return None
    return dest


def download_pdf(arxiv_id: str, cache, timeout: float = 120) -> Path | None:
    key = re.sub(r"[^0-9A-Za-z.]", "_", arxiv_id)
    blob = cache.blob_path("downloads", f"arxiv-pdf-{key}", "pdf")
    if blob.is_file():
        return blob
    try:
        r = httpx.get(f"https://arxiv.org/pdf/{arxiv_id}", headers=UA, timeout=timeout, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("could not download arXiv:%s PDF (%s)", arxiv_id, e)
        return None
    if not r.content.startswith(b"%PDF"):
        return None
    return cache.put_blob("downloads", f"arxiv-pdf-{key}", "pdf", r.content)
