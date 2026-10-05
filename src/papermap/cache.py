"""Content-addressed cache for stage outputs, LLM calls, downloads and audio.

Layout under the cache root::

    stages/<stage>/<key>.json   stage outputs (keyed by inputs + config + stage version)
    llm/<k[:2]>/<key>.json      individual LLM request/response pairs
    downloads/<key>.pdf         fetched papers
    audio/<key>.<ext>           synthesized narration clips

Keys are hashes of everything that influences a result, so a re-run with the
same paper/profile/config is served entirely from cache (reproducible), and a
run that failed halfway resumes from the last completed LLM call.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Cache:
    def __init__(self, root: str | Path, enabled: bool = True):
        self.root = Path(root)
        self.enabled = enabled
        # every entry this run read or wrote, so a project can list (and clear) its share of the cache
        self.touched: set[str] = set()
        self._touch_lock = threading.Lock()

    def touch(self, path: Path) -> Path:
        try:
            rel = path.relative_to(self.root).as_posix()
        except ValueError:
            return path
        with self._touch_lock:
            self.touched.add(rel)
        return path

    # --- stages
    def stage_path(self, stage: str, key: str) -> Path:
        return self.touch(self.root / "stages" / stage / f"{key}.json")

    def get_stage(self, stage: str, key: str) -> Any | None:
        if not self.enabled:
            return None
        p = self.stage_path(stage, key)
        if not p.is_file():
            return None
        try:
            return json.loads(p.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def put_stage(self, stage: str, key: str, data: Any) -> Path:
        p = self.stage_path(stage, key)
        _atomic_write(p, json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))
        return p

    # --- llm calls
    def llm_path(self, key: str) -> Path:
        return self.touch(self.root / "llm" / key[:2] / f"{key}.json")

    def get_llm(self, key: str) -> str | None:
        if not self.enabled:
            return None
        p = self.llm_path(key)
        if not p.is_file():
            return None
        try:
            return json.loads(p.read_text("utf-8"))["response"]
        except (OSError, json.JSONDecodeError, KeyError):
            return None

    def put_llm(self, key: str, request: dict[str, Any], response: str, meta: dict[str, Any] | None = None) -> Path:
        p = self.llm_path(key)
        record = {"request": request, "response": response, "meta": meta or {}}
        _atomic_write(p, json.dumps(record, ensure_ascii=False, indent=1).encode("utf-8"))
        return p

    # --- binary blobs
    def blob_path(self, kind: str, key: str, ext: str) -> Path:
        return self.touch(self.root / kind / f"{key}.{ext.lstrip('.')}")

    def put_blob(self, kind: str, key: str, ext: str, data: bytes) -> Path:
        p = self.blob_path(kind, key, ext)
        _atomic_write(p, data)
        return p
