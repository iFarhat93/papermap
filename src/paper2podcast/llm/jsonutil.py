"""Tolerant JSON extraction for model output."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


class JSONExtractionError(ValueError):
    pass


def strip_reasoning(text: str) -> str:
    text = _THINK.sub("", text)
    # an unterminated <think> block (model cut off) -> keep what follows the last tag
    if "<think>" in text.lower() and "</think>" not in text.lower():
        text = text[text.lower().rfind("<think>") + 7 :]
    return text.strip()


def _candidates(text: str) -> list[str]:
    out: list[str] = []
    for m in _FENCE.finditer(text):
        out.append(m.group(1).strip())
    out.append(text.strip())
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        out.append(text[start : end + 1])
    return out


def _repairs(s: str) -> list[str]:
    fixed = _TRAILING_COMMA.sub(r"\1", s)
    fixed2 = fixed.replace("“", '"').replace("”", '"')
    return [s, fixed, fixed2]


def extract_json(text: str) -> Any:
    text = strip_reasoning(text)
    last_err: Exception | None = None
    for cand in _candidates(text):
        for attempt in _repairs(cand):
            try:
                return json.loads(attempt)
            except json.JSONDecodeError as e:
                last_err = e
    snippet = text[:160].replace("\n", " ")
    raise JSONExtractionError(f"{last_err}; output started with: {snippet!r}")


def summarize_validation_error(err: ValidationError, limit: int = 8) -> list[str]:
    out = []
    for e in err.errors()[:limit]:
        loc = ".".join(str(p) for p in e.get("loc", ()))
        out.append(f"{loc or '<root>'}: {e.get('msg')}")
    return out
