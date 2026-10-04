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

    def exact(g: float, decimals: int) -> str | None:
        text = f"{g:.{decimals}f}"
        return text if abs(float(text) - g) < 1e-9 * max(1.0, abs(g)) else None  # never a lossy rounding

    for g in (f, f * 100, f / 100):  # 0.853 <-> 85.3: fractions written as percentages and back
        for decimals in range(0, 5):
            t = exact(g, decimals)
            if t:
                forms.add(t)
    if f == int(f):
        forms.add(str(int(f)))
        forms.add(f"{int(f):,}")
    if s.startswith("0."):
        forms.add(s[1:])  # ".853"
    return {x for x in forms if x and x not in ("0", "-0")}


_NUM_IN_TEXT = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\d])")


def _rounding_match(value: float | str, hay: str) -> bool:
    """'about 41%' is fine when the text says 41.2%: the value must be a rounding of a real number."""
    s = str(value).strip().rstrip("%").replace(",", "")
    try:
        f = float(s)
    except ValueError:
        return False
    decimals = len(s.split(".")[1]) if "." in s else 0
    for m in _NUM_IN_TEXT.finditer(hay):
        x = float(m.group(0))
        if x != f and round(x, decimals) == f and len(m.group(0).replace(".", "")) > len(s.replace(".", "")):
            return True
    return False


_TIMES = r"(?:[·x×*⋅]|\\cdot|\\times)?"


def _scientific_spans(value: float | str, hay: str) -> list[tuple[int, int]]:
    """Where 3.3e18 is written as "3.3 · 10^18", as LaTeX "3.3 \\cdot 10^{18}", or as the
    "3.3 · 1018" that PDF extraction flattens it to."""
    try:
        f = float(str(value).replace(",", ""))
    except ValueError:
        return []
    if f == 0 or 1e-3 <= abs(f) < 1e5:
        return []
    exp = int(f"{abs(f):e}".split("e")[1])
    mant = abs(f) / 10**exp
    spans = []
    for m in {f"{mant:.{d}f}".rstrip("0").rstrip(".") for d in range(0, 3)}:
        mant_re = "" if m == "1" else rf"{re.escape(m)}\s*{_TIMES}\s*"
        spans += [x.span() for x in re.finditer(rf"(?<![\d.]){mant_re}10\s*\^?\s*[({{]?\s*{exp}(?![\d])", hay)]
    return spans


def _scientific_match(value: float | str, hay: str) -> bool:
    return bool(_scientific_spans(value, hay))


def number_in_text(value: float | str | None, text: str) -> bool:
    if value is None:
        return True
    hay = normalize(text).replace(",", "")
    for form in _number_forms(value):
        if re.search(rf"(?<![\d.]){re.escape(form)}(?![\d])", hay):
            return True
    return _scientific_match(value, hay) or _rounding_match(value, hay)


_LABEL_STOP = {"the", "and", "with", "from", "for", "of", "on", "in", "to", "a", "an", "by", "vs", "at", "as", "model"}


def canon(text: str) -> str:
    """Comparable form for labels: "$\\pi_0$-FAST" and "π0-FAST" both become "pi0 fast"."""
    t = unicodedata.normalize("NFKC", text).lower().replace("π", "pi")
    t = re.sub(r"[\\$_^{}]", "", t)
    return re.sub(r"[^a-z0-9.%]+", " ", t).strip()


def label_matches(label: str, text: str) -> bool:
    lab = canon(label)
    if not lab:
        return False
    hay = canon(text)
    if lab in hay:
        return True
    tokens = [t for t in lab.split() if len(t) >= 2 and t not in _LABEL_STOP]
    if not tokens:
        return False
    have = set(hay.split())
    hit = sum(1 for t in tokens if t in have)
    return hit >= max(1, -(-len(tokens) * 6 // 10))  # at least 60% of the label's words


def number_near_label(value: float | str, label: str, text: str, tables=(), window: int = 220) -> bool:
    """Attribution check: the number must sit next to its label - in the same table
    row (or column), or within `window` characters of the label in the text."""
    if value is None or not str(label).strip():
        return True
    for t in tables:
        for row in t.rows:
            if row and (label_matches(label, row[0]) or label_matches(label, " ".join(row))) and number_in_text(value, " | ".join(row[1:])):
                return True
        for ci, col in enumerate(t.columns):
            if col and label_matches(label, col) and any(ci < len(r) and number_in_text(value, r[ci]) for r in t.rows):
                return True
    hay = normalize(text).replace(",", "")
    spans = [m.span() for form in _number_forms(value) for m in re.finditer(rf"(?<![\d.]){re.escape(form)}(?![\d])", hay)]
    for start, end in spans + _scientific_spans(value, hay):
        if label_matches(label, hay[max(0, start - window) : end + window]):
            return True
    return False


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
