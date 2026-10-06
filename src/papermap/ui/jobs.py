"""Background runs: a persistent queue of `papermap run` subprocesses.

Each run is its own process, so a crash or a cancel never touches the dashboard
or the other runs. Runs survive a restart of `papermap ui`: their process ids are
kept in jobs.json and picked up again. Completed LLM calls are cached, so
"resume" just starts the same command again.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from ..cache import _atomic_write
from ..cli import _slug, default_out_dir
from ..log import get_logger
from . import providers
from .projects import Library

log = get_logger("ui.jobs")

FINISHED = ("done", "failed", "cancelled", "interrupted")
_LOG_LINE = re.compile(r"^\S+ \S+ (\w+)\s+\S+ ?(\w*)\| (.*)$")


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class JobManager:
    def __init__(self, workspace: Path, library: Library, state_dir: Path, limits: dict[str, int]):
        self.workspace = Path(workspace)
        self.library = library
        self.path = Path(state_dir) / "jobs.json"
        self.limits = limits  # {"local": n, "cloud": n}, edited from the settings screen
        self.jobs: list[dict[str, Any]] = []
        self.procs: dict[str, subprocess.Popen] = {}
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self._load()
        threading.Thread(target=self._loop, name="papermap-jobs", daemon=True).start()

    # ------------------------------------------------------------ persistence
    def _load(self) -> None:
        try:
            rows = json.loads(self.path.read_text("utf-8")) if self.path.is_file() else []
        except (OSError, ValueError):
            rows = []
        for j in rows:
            if j.get("state") == "running" and not _pid_alive(j.get("pid")):
                j["state"] = "interrupted"
                j["finished"] = j.get("finished") or time.time()
                j["error"] = j.get("error") or "the run stopped while the dashboard was closed"
            self.jobs.append(j)

    def _save(self) -> None:
        with self.lock:
            data = json.dumps(self.jobs[-500:], ensure_ascii=False, indent=1).encode("utf-8")
        _atomic_write(self.path, data)

    # ---------------------------------------------------------------- submit
    def out_name(self, source: str, profile_path: str | None, provider: str, model: str, compare: bool) -> str:
        base = default_out_dir(source, profile_path).name
        if compare:
            return f"{base}--{_slug(model)}"[:120]
        # keep the CLI's folder name unless it already holds (or will hold) a page from another model
        d = self.library.root / base
        cfg_path = d / "papermap.config.json"
        other = None
        if cfg_path.is_file():
            try:
                llm = json.loads(cfg_path.read_text("utf-8")).get("llm") or {}
                other = (llm.get("provider"), llm.get("model"))
            except (OSError, ValueError):
                pass
        with self.lock:
            for j in self.jobs:
                if j["out"] == base and j["state"] not in FINISHED:
                    other = (j["provider"], j["model"])
        if other and other != (provider, model):
            return f"{base}--{_slug(model)}"[:120]
        return base

    def submit(self, *, source: str, label: str, profile: str | None, profile_path: str | None,
               config: dict[str, Any], compare: bool, pages: int | None) -> dict:
        llm = config["llm"]
        name = self.out_name(source, profile_path, llm["provider"], llm["model"], compare)
        job = {
            "id": uuid.uuid4().hex[:10], "created": time.time(), "source": source, "label": label,
            "profile": profile, "profile_path": profile_path, "out": name, "config": config,
            "provider": llm["provider"], "model": llm["model"], "base_url": llm.get("base_url"),
            "lane": "local" if providers.is_local(llm["provider"], llm.get("base_url")) else "cloud",
            "pages": pages, "state": "queued", "pid": None, "started": None, "finished": None,
            "returncode": None, "error": None, "attempts": 0,
        }
        with self.lock:
            self.jobs.append(job)
        self._save()
        self.wake.set()
        return job

    # --------------------------------------------------------------- control
    def find(self, job_id: str) -> dict | None:
        with self.lock:
            return next((j for j in self.jobs if j["id"] == job_id), None)

    def cancel(self, job_id: str) -> bool:
        with self.lock:
            j = self.find(job_id)
            if not j or j["state"] in FINISHED:
                return False
            if j["state"] == "running":
                proc = self.procs.pop(job_id, None)
                try:
                    if proc:
                        proc.terminate()
                    elif _pid_alive(j["pid"]):
                        os.kill(j["pid"], signal.SIGTERM)
                except OSError:
                    pass
            j["state"] = "cancelled"
            j["finished"] = time.time()
        self._save()
        return True

    def resume(self, job_id: str) -> bool:
        with self.lock:
            j = self.find(job_id)
            if not j or j["state"] not in FINISHED:
                return False
            j.update(state="queued", pid=None, started=None, finished=None, returncode=None, error=None, created=time.time())
        self._save()
        self.wake.set()
        return True

    def remove(self, job_id: str) -> bool:
        with self.lock:
            j = self.find(job_id)
            if not j or j["state"] == "running":
                return False
            self.jobs.remove(j)
        self._save()
        return True

    def clear_finished(self) -> int:
        """Forget finished and cancelled runs; failed and interrupted ones stay so they can be resumed."""
        with self.lock:
            before = len(self.jobs)
            self.jobs = [j for j in self.jobs if j["state"] not in ("done", "cancelled")]
            n = before - len(self.jobs)
        self._save()
        return n

    def busy(self) -> bool:
        with self.lock:
            return any(j["state"] == "running" for j in self.jobs)

    # ----------------------------------------------------------------- worker
    def _loop(self) -> None:
        while True:
            try:
                self._tick()
            except Exception:  # keep the queue alive whatever happens
                log.exception("job loop error")
            self.wake.wait(1.0)
            self.wake.clear()

    def _tick(self) -> None:
        changed = False
        with self.lock:
            for j in self.jobs:
                if j["state"] != "running":
                    continue
                proc = self.procs.get(j["id"])
                code = proc.poll() if proc else (None if _pid_alive(j["pid"]) else -1)
                if code is None:
                    continue
                self.procs.pop(j["id"], None)
                self._finish(j, code)
                changed = True
            running = {lane: sum(1 for j in self.jobs if j["state"] == "running" and j["lane"] == lane) for lane in ("local", "cloud")}
            for j in self.jobs:
                if j["state"] != "queued":
                    continue
                if running[j["lane"]] >= max(1, int(self.limits.get(j["lane"], 1))):
                    continue
                self._start(j)
                running[j["lane"]] += 1
                changed = True
        if changed:
            self._save()

    def _start(self, j: dict) -> None:
        out = self.library.root / j["out"]
        logs = out / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "status.json").unlink(missing_ok=True)
        cfg_path = logs / "ui-run-config.json"
        cfg_path.write_text(json.dumps(j["config"], ensure_ascii=False, indent=1), "utf-8")
        cmd = [sys.executable, "-m", "papermap", "run", j["source"], "--out", str(out), "-c", str(cfg_path)]
        if j.get("profile_path"):
            cmd += ["--profile", j["profile_path"]]
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        console = open(logs / "ui-console.log", "ab")
        try:
            proc = subprocess.Popen(
                cmd, cwd=self.workspace, env=env, stdout=console, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                start_new_session=True,  # a Ctrl+C on the dashboard does not kill the runs
            )
        except OSError as e:
            j.update(state="failed", error=f"could not start: {e}", finished=time.time())
            return
        finally:
            console.close()
        self.procs[j["id"]] = proc
        j.update(state="running", pid=proc.pid, started=time.time(), attempts=j.get("attempts", 0) + 1)
        log.info("started %s (%s/%s) pid %d", j["out"], j["provider"], j["model"], proc.pid)

    def _finish(self, j: dict, code: int) -> None:
        status = self.status(j)
        j["finished"] = time.time()
        j["returncode"] = code
        if status.get("state") == "done" and code in (0, -1):
            j["state"] = "done"
            j["error"] = None
        else:
            j["state"] = "failed"
            j["error"] = status.get("error") or self._console_error(j) or f"the run exited with code {code}"
            j["failed_stage"] = status.get("failed_stage")
        log.info("%s %s", j["out"], j["state"])

    # ------------------------------------------------------------------ views
    def status(self, j: dict) -> dict:
        try:
            return json.loads((self.library.root / j["out"] / "logs" / "status.json").read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def _console_error(self, j: dict) -> str:
        lines = [ln for ln in self._tail(self.library.root / j["out"] / "logs" / "ui-console.log", 40) if ln.strip()]
        errs = [ln for ln in lines if "error" in ln.lower()]
        return (errs or lines or [""])[-1].strip()[:500]

    @staticmethod
    def _tail(path: Path, n: int) -> list[str]:
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 64_000))
                return f.read().decode("utf-8", "replace").splitlines()[-n:]
        except OSError:
            return []

    def log_lines(self, job_id: str, n: int = 80) -> list[str]:
        """The run's INFO-and-above lines, as the console shows them."""
        j = self.find(job_id)
        if not j:
            return []
        out = []
        for line in self._tail(self.library.root / j["out"] / "logs" / "run.log", 600):
            m = _LOG_LINE.match(line)
            if not m:
                continue
            level, stage, msg = m.groups()
            if level == "DEBUG":
                continue
            prefix = f"[{stage}] " if stage else ""
            out.append(f"{prefix}{'' if level == 'INFO' else level.lower() + ': '}{msg}")
        if not out:
            out = [ln for ln in self._tail(self.library.root / j["out"] / "logs" / "ui-console.log", n) if ln.strip()]
        return out[-n:]

    def view(self) -> list[dict]:
        with self.lock:
            jobs = [dict(j) for j in self.jobs]
        queued_pos: dict[str, int] = {}
        for lane in ("local", "cloud"):
            for i, j in enumerate(x for x in jobs if x["state"] == "queued" and x["lane"] == lane):
                queued_pos[j["id"]] = i + 1
        rows = []
        for j in jobs:
            st = self.status(j) if j["state"] != "queued" else {}
            usage = st.get("usage") or []
            tin = sum(u.get("input_tokens", 0) for u in usage)
            tout = sum(u.get("output_tokens", 0) for u in usage)
            costs = [providers.cost(u.get("provider", j["provider"]), u.get("model", j["model"]), u.get("input_tokens", 0), u.get("output_tokens", 0)) for u in usage]
            end = j["finished"] or (time.time() if j["state"] == "running" else None)
            rows.append({
                **{k: j.get(k) for k in ("id", "label", "source", "out", "profile", "provider", "model", "lane", "state",
                                         "created", "started", "finished", "error", "failed_stage", "attempts", "pages")},
                "position": queued_pos.get(j["id"]),
                "elapsed": round(end - j["started"], 1) if j["started"] and end else None,
                "stages": st.get("stages") or [],
                "done": st.get("done") or [],
                "cached": st.get("cached") or [],
                "current": st.get("current") if j["state"] == "running" else None,
                "tokens": {"input": tin, "output": tout, "calls": sum(u.get("calls", 0) for u in usage)},
                "cost": None if any(c is None for c in costs) else round(sum(costs), 4),
                "estimate": providers.estimate(j["provider"], j["model"], j.get("base_url"), j.get("pages")),
            })
        rows.sort(key=lambda r: ({"running": 0, "queued": 1}.get(r["state"], 2), -(r["created"] or 0)))
        return rows
