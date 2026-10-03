"""Tiny dependency-free BM25 index for grounding Q&A in the paper text."""

from __future__ import annotations

import math
import re
from collections import Counter

_STOP = set(
    """a an and are as at be but by for from has have how i in is it its of on or that the their this to was
    were what when where which who why will with does do did can could should would about into than then
    there these those they them we our you your not no if so such also more most""".split()
)


def tokenize(text: str) -> list[str]:
    toks = re.findall(r"[a-z0-9][a-z0-9\-+.]*[a-z0-9+]|[a-z0-9]", text.lower())
    return [t for t in toks if t not in _STOP]


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = [Counter(tokenize(d)) for d in docs]
        self.lengths = [sum(c.values()) for c in self.docs]
        self.avg = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0.0
        df: Counter[str] = Counter()
        for c in self.docs:
            df.update(c.keys())
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: str) -> list[float]:
        q = tokenize(query)
        out = []
        for c, length in zip(self.docs, self.lengths):
            s = 0.0
            for t in q:
                f = c.get(t)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * length / (self.avg or 1))
                s += self.idf.get(t, 0.0) * f * (self.k1 + 1) / denom
            out.append(s)
        return out

    def top(self, query: str, k: int = 5) -> list[tuple[int, float]]:
        scored = [(i, s) for i, s in enumerate(self.scores(query)) if s > 0]
        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored[:k]
