"""The library: every output folder under the workspace, read without loading models."""

from __future__ import annotations

import base64
import io
import json
import mimetypes
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any

from ..cli import default_cache_dir

NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
# Folders that belong to the run, not to the page someone else would open.
PRIVATE_DIRS = ("logs", "debug")


def _load(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return default


def _concept_key(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()


class Library:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._meta: dict[str, tuple[float, dict]] = {}  # name -> (experience mtime, summary)

    def path(self, name: str) -> Path | None:
        if not NAME_RE.match(name or "") or name.startswith("."):
            return None
        p = self.root / name
        return p if p.is_dir() else None

    def names(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir() and NAME_RE.match(p.name) and not p.name.startswith("."))

    # ------------------------------------------------------------- summaries
    def _experience_summary(self, d: Path) -> dict:
        exp_path = d / "experience.json"
        if not exp_path.is_file():
            return {}
        mtime = exp_path.stat().st_mtime
        cached = self._meta.get(d.name)
        if cached and cached[0] == mtime:
            return cached[1]
        exp = _load(exp_path, {}) or {}
        paper = exp.get("paper") or {}
        graph = exp.get("graph") or {}
        summary = {
            "title": paper.get("title") or d.name,
            "authors": paper.get("authors") or [],
            "method": paper.get("method_name") or "",
            "one_line": paper.get("one_line") or "",
            "source": paper.get("source") or "",
            "arxiv_id": paper.get("arxiv_id") or "",
            "pages": paper.get("n_pages") or 0,
            "sections": len(exp.get("sections") or []),
            "quiz_questions": len((exp.get("quiz") or {}).get("questions") or []),
            "reader": (exp.get("profile") or {}).get("name") or "",
            "generator": exp.get("generator") or "",
            "concepts": [
                {"label": n.get("label", ""), "type": n.get("type", "concept"), "aliases": n.get("aliases") or [],
                 "description": n.get("description", "")}
                for n in graph.get("nodes") or [] if n.get("label")
            ],
        }
        self._meta[d.name] = (mtime, summary)
        return summary

    def summary(self, name: str, with_concepts: bool = False) -> dict | None:
        d = self.path(name)
        if d is None:
            return None
        exp = dict(self._experience_summary(d))
        concepts = exp.pop("concepts", [])
        cfg = _load(d / "papermap.config.json", {}) or {}
        llm = cfg.get("llm") or {}
        status = _load(d / "logs" / "status.json", {}) or {}
        quiz = [r for r in _load(d / "quiz_results.json", []) or [] if isinstance(r, dict)]
        reading = _load(d / "reading.json", {}) or {}
        index = d / "index.html"
        out = {
            "name": name,
            "ready": index.is_file() and bool(exp),
            "title": exp.get("title") or (status.get("source") or name).rstrip("/").split("/")[-1],
            **{k: v for k, v in exp.items() if k != "title"},
            "provider": llm.get("provider") or "",
            "model": llm.get("model") or "",
            "base_url": llm.get("base_url") or "",
            "updated": index.stat().st_mtime if index.is_file() else (status.get("updated") or d.stat().st_mtime),
            "status": {k: status.get(k) for k in ("state", "current", "done", "error", "failed_stage", "source")} if status else None,
            "quiz": {
                "attempts": len(quiz),
                "last": {k: quiz[-1].get(k) for k in ("correct", "total", "at")} if quiz else None,
                "best": max(({"correct": r.get("correct", 0), "total": r.get("total", 0)} for r in quiz), key=lambda r: r["correct"]) if quiz else None,
                "missed": quiz[-1].get("missed", []) if quiz else [],
            },
            "reading": {"read": len(reading.get("read") or []), "total": reading.get("total") or exp.get("sections") or 0,
                        "at": reading.get("at")} if reading else None,
        }
        if with_concepts:
            out["concepts"] = concepts
        return out

    def all(self) -> list[dict]:
        rows = [s for s in (self.summary(n) for n in self.names()) if s]
        return sorted(rows, key=lambda r: r.get("updated") or 0, reverse=True)

    # ------------------------------------------------------- cross-paper views
    def search(self, query: str) -> list[dict]:
        terms = [t for t in _concept_key(query).split() if t]
        if not terms:
            return []
        hits = []
        for name in self.names():
            s = self.summary(name, with_concepts=True)
            if not s:
                continue
            text = _concept_key(" ".join([s.get("title", ""), s.get("method", ""), s.get("one_line", ""), " ".join(s.get("authors") or [])]))
            matched = [
                c["label"] for c in s.get("concepts", [])
                if all(t in _concept_key(" ".join([c["label"], *c.get("aliases", []), c.get("description", "")])) for t in terms)
            ]
            in_meta = all(t in text for t in terms)
            if in_meta or matched:
                hits.append({"name": name, "title": s["title"], "in_title": in_meta, "concepts": matched[:12]})
        return sorted(hits, key=lambda h: (not h["in_title"], -len(h["concepts"])))

    def connections(self, min_papers: int = 2) -> list[dict]:
        """Concepts, methods and datasets that appear in more than one paper. Several pages
        of the same paper (other model, other reader) count as one paper."""
        seen: dict[str, dict] = {}
        for name in self.names():
            s = self.summary(name, with_concepts=True)
            if not s:
                continue
            paper = s.get("arxiv_id") or _concept_key(s["title"])
            for c in s.get("concepts", []):
                if c["type"] == "this_work":  # every graph's centre node: the paper itself
                    continue
                keys = {_concept_key(c["label"]), *(_concept_key(a) for a in c.get("aliases", []))} - {""}
                entry = next((seen[k] for k in keys if k in seen), None)
                if entry is None:
                    entry = {"label": c["label"], "type": c["type"], "papers": {}}
                for k in keys:
                    seen[k] = entry
                entry["papers"].setdefault(paper, {"name": name, "title": s["title"]})
        unique = {id(e): e for e in seen.values()}.values()
        rows = [{"label": e["label"], "type": e["type"], "papers": list(e["papers"].values())}
                for e in unique if len(e["papers"]) >= min_papers]
        return sorted(rows, key=lambda r: (-len(r["papers"]), r["label"].lower()))

    # ------------------------------------------------------------- sharing
    def zip_bytes(self, name: str) -> bytes:
        d = self.path(name)
        if d is None:
            raise FileNotFoundError(name)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(d.rglob("*")):
                rel = f.relative_to(d)
                if f.is_file() and rel.parts[0] not in PRIVATE_DIRS and rel.name not in ("quiz_results.json", "reading.json"):
                    z.write(f, f"{name}/{rel.as_posix()}")
        return buf.getvalue()

    def single_html(self, name: str) -> str:
        """index.html with figures, page images and audio inlined as data URIs: one file to send."""
        d = self.path(name)
        if d is None or not (d / "index.html").is_file():
            raise FileNotFoundError(name)
        html = (d / "index.html").read_text("utf-8")
        for f in sorted(d.rglob("*")):
            rel = f.relative_to(d)
            if not f.is_file() or rel.parts[0] in PRIVATE_DIRS or len(rel.parts) < 2:
                continue
            needle = f'"{rel.as_posix()}"'
            if needle not in html:
                continue
            mime = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            uri = f"data:{mime};base64,{base64.b64encode(f.read_bytes()).decode('ascii')}"
            html = html.replace(needle, f'"{uri}"')
        return html

    def delete(self, name: str) -> bool:
        d = self.path(name)
        if d is None or d.resolve().parent != self.root.resolve():
            return False
        shutil.rmtree(d)
        self._meta.pop(name, None)
        return True


# ----------------------------------------------------------------------- cache


class CacheInfo:
    """Disk use of the shared cache, overall and per project (from logs/cache_files.json)."""

    def __init__(self, library: Library, root: Path | None = None):
        self.library = library
        self.root = Path(root) if root else default_cache_dir()

    def _manifest(self, name: str) -> set[str]:
        d = self.library.path(name)
        return set(_load(d / "logs" / "cache_files.json", []) or []) if d else set()

    def _size(self, rel: str) -> int:
        p = self.root / rel
        if p.is_file():
            return p.stat().st_size
        if p.is_dir():
            return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        return 0

    def overview(self) -> dict:
        kinds = []
        if self.root.is_dir():
            for k in sorted(p for p in self.root.iterdir() if p.is_dir()):
                files = [f for f in k.rglob("*") if f.is_file()]
                kinds.append({"kind": k.name, "files": len(files), "bytes": sum(f.stat().st_size for f in files)})
        manifests = {n: self._manifest(n) for n in self.library.names()}
        projects = []
        for name, files in manifests.items():
            others = set().union(*(m for n, m in manifests.items() if n != name)) if len(manifests) > 1 else set()
            existing = [f for f in files if (self.root / f).exists()]
            projects.append({
                "name": name, "files": len(existing), "bytes": sum(self._size(f) for f in existing),
                "own_bytes": sum(self._size(f) for f in existing if f not in others), "tracked": bool(files),
            })
        return {"root": str(self.root), "total": sum(k["bytes"] for k in kinds), "kinds": kinds,
                "projects": sorted(projects, key=lambda p: -p["bytes"])}

    def clear_project(self, name: str) -> int:
        """Delete this project's cache entries that no other project uses. Returns bytes freed."""
        mine = self._manifest(name)
        others = set().union(*(self._manifest(n) for n in self.library.names() if n != name))
        freed = 0
        for rel in sorted(mine - others):
            p = (self.root / rel).resolve()
            if not p.is_relative_to(self.root.resolve()):
                continue
            freed += self._size(rel)
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.is_file():
                p.unlink(missing_ok=True)
        return freed

    def clear_kind(self, kind: str) -> int:
        if not NAME_RE.match(kind or ""):
            return 0
        p = self.root / kind
        if not p.is_dir():
            return 0
        freed = self._size(kind)
        shutil.rmtree(p, ignore_errors=True)
        return freed
