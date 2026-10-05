"""Local server: serves a generated experience and its grounded Q&A endpoint.

    GET  /               the experience (index.html + audio)
    GET  /api/health     {"ok": true, "provider": ..., "model": ...}
    POST /api/ask        {"question": str, "context": {...}, "history": [...]} -> {"answer", "refs", "titles"}
    POST /api/quiz       a finished quiz attempt, appended to quiz_results.json
    POST /api/progress   the sections read so far, saved to reading.json

The web UI (``papermap ui``) serves many pages at once and reuses the helpers below.

Local-first and single-user: binds to 127.0.0.1 by default. No production
deployment assumptions (no auth, no rate limiting).
"""

from __future__ import annotations

import json
import threading
import time
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .log import get_logger
from .qa import QAEngine

log = get_logger("server")
MAX_BODY = 256 * 1024


def read_json(handler: SimpleHTTPRequestHandler, limit: int = MAX_BODY) -> tuple[dict | None, str]:
    """Parse a JSON object request body; returns (body, error)."""
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        return None, "bad Content-Length"
    if length <= 0 or length > limit:
        return None, "request too large" if length > limit else "empty request"
    try:
        req = json.loads(handler.rfile.read(length).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        return None, f"bad request: {e}"
    return (req, "") if isinstance(req, dict) else (None, "bad request: expected a JSON object")


def answer(engine: QAEngine | None, req: dict) -> tuple[int, dict]:
    if engine is None:
        return 503, {"error": "Q&A is not configured on this server"}
    question = str(req.get("question", ""))
    ctx = req.get("context") if isinstance(req.get("context"), dict) else {}
    history = req.get("history") if isinstance(req.get("history"), list) else []
    try:
        result = engine.answer(question, ctx, history)
    except Exception as e:  # report model/provider failures to the page
        log.warning("Q&A failed: %s", e)
        return 502, {"error": str(e)}
    log.info("Q: %s -> %d chars, refs %s", question[:80], len(result["answer"]), result["refs"])
    return 200, result


_activity_lock = threading.Lock()


def record_activity(out_dir: Path, kind: str, req: dict) -> tuple[int, dict]:
    """Keep what the reader did on the page next to it, so the web UI can show it."""
    if kind == "quiz":
        try:
            entry = {
                "correct": int(req["correct"]), "total": int(req["total"]), "at": time.time(),
                "missed": [
                    {"id": str(m.get("id", "")), "section_id": str(m.get("section_id", "")), "question": str(m.get("question", ""))[:400]}
                    for m in (req.get("missed") or [])[:100] if isinstance(m, dict)
                ],
            }
        except (KeyError, TypeError, ValueError):
            return 400, {"error": "bad quiz result"}
        path = Path(out_dir) / "quiz_results.json"
        with _activity_lock:
            try:
                rows = json.loads(path.read_text("utf-8")) if path.is_file() else []
            except (OSError, ValueError):
                rows = []
            rows = [r for r in rows if isinstance(r, dict)][-199:] + [entry]
            path.write_text(json.dumps(rows, ensure_ascii=False, indent=1), "utf-8")
        return 200, {"ok": True}
    if kind == "progress":
        read = [str(x) for x in (req.get("read") or [])[:500]]
        try:
            total = int(req.get("total") or 0)
        except (TypeError, ValueError):
            return 400, {"error": "bad progress"}
        entry = {"read": read, "total": total, "at": time.time()}
        with _activity_lock:
            (Path(out_dir) / "reading.json").write_text(json.dumps(entry, ensure_ascii=False), "utf-8")
        return 200, {"ok": True}
    return 404, {"error": "not found"}


class _Handler(SimpleHTTPRequestHandler):
    engine: QAEngine | None = None
    info: dict = {}
    out_dir: Path = Path(".")

    def log_message(self, fmt, *args):  # route http.server logs through our logger
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.split("?")[0].rstrip("/").endswith("/api/health"):
            return self._json(200, {"ok": self.engine is not None, **self.info})
        return super().do_GET()

    def do_POST(self):  # noqa: N802
        route = self.path.split("?")[0].rstrip("/").rsplit("/api/", 1)[-1]
        if route not in ("ask", "quiz", "progress"):
            return self._json(404, {"error": "not found"})
        req, err = read_json(self)
        if req is None:
            return self._json(413 if "too large" in err else 400, {"error": err})
        if route == "ask":
            return self._json(*answer(self.engine, req))
        return self._json(*record_activity(self.out_dir, route, req))


def serve(out_dir: Path, engine: QAEngine | None, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True,
          reason: str = "") -> None:
    """`reason` says why Q&A is off; the page shows it instead of a generic message."""
    out_dir = Path(out_dir).resolve()
    if not (out_dir / "index.html").is_file():
        raise FileNotFoundError(f"{out_dir} has no index.html - generate it first with `papermap run`")
    handler = partial(_Handler, directory=str(out_dir))
    _Handler.engine = engine
    _Handler.out_dir = out_dir
    _Handler.info = (
        {"provider": engine.llm.provider.name, "model": engine.llm.settings.model} if engine else ({"reason": reason} if reason else {})
    )
    server = None
    for p in range(port, port + 20):
        try:
            server = ThreadingHTTPServer((host, p), handler)
            port = p
            break
        except OSError:
            continue
    if server is None:
        raise OSError(f"no free port in {port}-{port + 19}")
    url = f"http://{host}:{port}/"
    log.info("serving %s at %s (Q&A %s) - Ctrl+C to stop", out_dir.name, url, "on" if engine else "off")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("stopped")
    finally:
        server.server_close()
