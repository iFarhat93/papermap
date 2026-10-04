"""Local server: serves a generated experience and its grounded Q&A endpoint.

    GET  /               the experience (index.html + audio)
    GET  /api/health     {"ok": true, "memory": bool, "provider": ..., "model": ...}
    POST /api/ask        {"question": str, "context": {...}, "history": [...]} -> {"answer", "refs", "titles"}
    POST /api/memory     {"kind": "quiz" | "finished" | "level", "data": {...}} -> {"ok": true}

Local-first and single-user: binds to 127.0.0.1 by default. No production
deployment assumptions (no auth, no rate limiting).
"""

from __future__ import annotations

import json
import threading
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .log import get_logger
from .memory import Recorder
from .qa import QAEngine

log = get_logger("server")
MAX_BODY = 256 * 1024


class _Handler(SimpleHTTPRequestHandler):
    engine: QAEngine | None = None
    recorder: Recorder | None = None
    info: dict = {}

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

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            raise ValueError("empty or too large")
        req = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(req, dict):
            raise ValueError("expected a JSON object")
        return req

    def do_GET(self):  # noqa: N802
        if self.path.split("?")[0].rstrip("/").endswith("/api/health"):
            return self._json(200, {"ok": self.engine is not None, "memory": self.recorder is not None, **self.info})
        return super().do_GET()

    def do_POST(self):  # noqa: N802
        route = self.path.split("?")[0].rstrip("/")
        if route.endswith("/api/memory"):
            return self._memory()
        if not route.endswith("/api/ask"):
            return self._json(404, {"error": "not found"})
        if self.engine is None:
            return self._json(503, {"error": "Q&A is not configured on this server"})
        try:
            req = self._body()
            question = str(req.get("question", ""))
            ctx = req.get("context") if isinstance(req.get("context"), dict) else {}
            history = req.get("history") if isinstance(req.get("history"), list) else []
        except (ValueError, UnicodeDecodeError) as e:
            return self._json(400, {"error": f"bad request: {e}"})
        try:
            result = self.engine.answer(question, ctx, history)
        except Exception as e:  # report model/provider failures to the page
            log.warning("Q&A failed: %s", e)
            return self._json(502, {"error": str(e)})
        log.info("Q: %s -> %d chars, refs %s", question[:80], len(result["answer"]), result["refs"])
        if self.recorder:
            try:
                self.recorder.asked(question, ctx.get("section_id"))
            except OSError as e:
                log.warning("reader memory not updated: %s", e)
        return self._json(200, result)

    def _memory(self):
        """What the reader did on the page (quiz answers, finishing, a level rating)."""
        if self.recorder is None:
            return self._json(503, {"error": "reader memory is off on this server"})
        try:
            req = self._body()
            self.recorder.record(str(req.get("kind", "")), req.get("data") if isinstance(req.get("data"), dict) else {})
        except (ValueError, UnicodeDecodeError) as e:
            return self._json(400, {"error": f"bad request: {e}"})
        except OSError as e:
            log.warning("reader memory not updated: %s", e)
            return self._json(500, {"error": "could not write the reader memory"})
        return self._json(200, {"ok": True})


def make_server(out_dir: Path, engine: QAEngine | None, host: str = "127.0.0.1", port: int = 8765, reason: str = "",
                recorder: Recorder | None = None) -> tuple[ThreadingHTTPServer, str]:
    """`reason` says why Q&A is off (the page shows it); `recorder` updates the reader's memory."""
    out_dir = Path(out_dir).resolve()
    if not (out_dir / "index.html").is_file():
        raise FileNotFoundError(f"{out_dir} has no index.html - generate it first with `papermap run`")
    handler = partial(_Handler, directory=str(out_dir))
    _Handler.engine = engine
    _Handler.recorder = recorder
    _Handler.info = (
        {"provider": engine.llm.provider.name, "model": engine.llm.settings.model} if engine else ({"reason": reason} if reason else {})
    )
    for p in range(port, port + 20):
        try:
            return ThreadingHTTPServer((host, p), handler), f"http://{host}:{p}/"
        except OSError:
            continue
    raise OSError(f"no free port in {port}-{port + 19}")


def serve(out_dir: Path, engine: QAEngine | None, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True,
          reason: str = "", recorder: Recorder | None = None) -> None:
    server, url = make_server(out_dir, engine, host, port, reason, recorder)
    log.info("serving %s at %s (Q&A %s, reader memory %s) - Ctrl+C to stop", Path(out_dir).resolve().name, url,
             "on" if engine else "off", f"on for {recorder.reader!r}" if recorder else "off")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("stopped")
    finally:
        server.server_close()
