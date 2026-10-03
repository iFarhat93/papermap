"""Grounding checks: keep generated content tied to the actual paper text.

* quotes must occur (near-)verbatim in the source -> otherwise dropped
* numbers in charts/tables/results must appear in the source -> otherwise rejected
* entity names must appear in the paper -> otherwise dropped from the graph
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

_TRANSLATE = str.maketrans(
    {
        "‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
        "−": "-", " ": " ", " ": " ", " ": " ", "­": "",
    }
)
_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Display-preserving normalization (case kept)."""
    text = unicodedata.normalize("NFKC", text).translate(_TRANSLATE)
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)  # de-hyphenate line breaks
    return _WS.sub(" ", text).strip()


def _key(text: str) -> str:
    return normalize(text).lower()


def find_quote(quote: str, source: str, min_ratio: float = 0.88) -> str | None:
    """Return the quote as it appears in ``source`` (cleaned), or None.

    Exact (normalized) matches pass directly; otherwise we accept a near-match
    covering >= ``min_ratio`` of the quote, which tolerates PDF extraction noise
    (ligatures, hyphenation, dropped symbols) but not paraphrase.
    """
    q = normalize(quote).strip(" \"'")
    if len(q) < 12:
        return None
    src = normalize(source)
    q_low, src_low = q.lower(), src.lower()
    idx = src_low.find(q_low)
    if idx != -1:
        return src[idx : idx + len(q)]
    # near-match: anchor on the longest common block, then score the window
    sm = SequenceMatcher(None, src_low, q_low, autojunk=False)
    m = sm.find_longest_match(0, len(src_low), 0, len(q_low))
    if m.size < 8:
        return None
    start = max(0, m.a - m.b)
    end = min(len(src_low), start + len(q_low) + 8)
    window = src_low[start:end]
    ratio = SequenceMatcher(None, window, q_low, autojunk=False).ratio()
    if ratio >= min_ratio:
        return src[start : start + len(q_low)].strip()
    return None


def _number_forms(value: float | str) -> set[str]:
    forms: set[str] = set()
    s = str(value).strip().rstrip("%").replace(",", "")
    if not s:
        return forms
    forms.add(s)
    try:
        f = float(s)
    except ValueError:
        return forms
    for decimals in range(0, 5):
        forms.add(f"{f:.{decimals}f}")
    if f == int(f):
        forms.add(str(int(f)))
        forms.add(f"{int(f):,}")
    # 0.853 <-> 85.3 (fractions written as percentages and vice versa)
    for g in (f * 100, f / 100):
        for decimals in range(0, 4):
            forms.add(f"{g:.{decimals}f}")
    if s.startswith("0."):
        forms.add(s[1:])  # ".853"
    return {x for x in forms if x and x not in ("0", "-0")}


def _scientific_match(value: float | str, hay: str) -> bool:
    """3.3e18 written as "3.3 · 10^18" - which PDF extraction flattens to "3.3 · 1018"."""
    try:
        f = float(str(value).replace(",", ""))
    except ValueError:
        return False
    if f == 0 or 1e-3 <= abs(f) < 1e5:
        return False
    exp = int(f"{abs(f):e}".split("e")[1])
    mant = abs(f) / 10**exp
    mantissas = {f"{mant:.{d}f}".rstrip("0").rstrip(".") for d in range(0, 3)}
    for m in mantissas:
        mant_re = "" if m == "1" else rf"{re.escape(m)}\s*[·x×*⋅]?\s*"
        if re.search(rf"(?<![\d.]){mant_re}10\s*\^?\s*\(?{exp}(?![\d])", hay):
            return True
    return False


def number_in_text(value: float | str | None, text: str) -> bool:
    if value is None:
        return True
    hay = normalize(text).replace(",", "")
    for form in _number_forms(value):
        if re.search(rf"(?<![\d.]){re.escape(form)}(?![\d])", hay):
            return True
    return _scientific_match(value, hay)


def name_in_text(name: str, text_lower: str) -> bool:
    """Whether an entity name (or its acronym in parentheses) occurs in the text."""
    n = _key(name)
    if not n:
        return False
    if n in text_lower:
        return True
    m = re.search(r"\(([^)]{2,20})\)", name)
    if m and _key(m.group(1)) in text_lower:
        return True
    base = re.sub(r"\s*\([^)]*\)", "", n).strip()
    return bool(base) and base in text_lower


def lower_text(text: str) -> str:
    return _key(text)
