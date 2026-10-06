"""`papermap ui`: a local dashboard to configure models, queue papers and open results.

Local-first, single-user and stdlib-only like `papermap serve`. Protection against
other websites open in the same browser: the server binds to 127.0.0.1, only
answers requests whose Host is that address, rejects cross-origin writes, and
requires a token (given in the URL it prints, then kept in a SameSite=Strict cookie).
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import threading
import tomllib
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from .. import __version__
from ..cache import Cache, _atomic_write
from ..cli import EXAMPLE_PROFILE, default_cache_dir
from ..config import Config, LLMSettings, dump_toml, find_config, load_config
from ..log import get_logger
from ..server import answer, read_json, record_activity
from . import providers
from .jobs import JobManager
from .projects import NAME_RE, CacheInfo, Library

log = get_logger("ui")
COOKIE = "papermap_ui"
MAX_UPLOAD = 200 * 1024 * 1024
GLOBAL_CONFIG = Path.home() / ".config" / "papermap" / "config.toml"


def _asset(name: str) -> bytes:
    return resources.files("papermap").joinpath("web", "ui", name).read_bytes()


class App:
    """Everything the request handler needs, shared across threads."""

    def __init__(self, workspace: Path, port: int, host: str):
        self.workspace = workspace.resolve()
        self.state_dir = self.workspace / ".papermap-ui"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.library = Library(self.workspace / "papermap-out")
        self.library.root.mkdir(parents=True, exist_ok=True)
        self.profiles_dir = self.workspace / "profiles"
        self.uploads_dir = self.state_dir / "uploads"
        self.cache_dir = default_cache_dir()
        self.cache = CacheInfo(self.library, self.cache_dir)
        self.keys = providers.KeyStore()
        self.ui_path = self.state_dir / "ui.json"
        ui = self._load_ui()
        self.limits = {"local": 1, "cloud": 3, **(ui.get("limits") or {})}
        self.jobs = JobManager(self.workspace, self.library, self.state_dir, self.limits)
        self.token = self._token()
        self.port = port
        self.host = host
        self.engines: dict[str, tuple[float, Any, str]] = {}
        self.engine_lock = threading.Lock()

    def _token(self) -> str:
        path = self.state_dir / "token"
        if path.is_file():
            t = path.read_text().strip()
            if len(t) >= 20:
                return t
        t = secrets.token_urlsafe(24)
        _atomic_write(path, t.encode())
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return t

    def _load_ui(self) -> dict:
        try:
            return json.loads(self.ui_path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def allowed_hosts(self) -> set[str]:
        hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"}
        if self.host not in ("127.0.0.1", "localhost", "::1"):
            hosts.add(f"{self.host}:{self.port}")
        return hosts

    # --------------------------------------------------------------- config
    def config_path(self) -> Path:
        found = find_config(None)
        return found if found and found.suffix == ".toml" else GLOBAL_CONFIG

    def settings(self) -> dict:
        config, found = load_config(None)
        provider = config.llm.provider
        envs = {name: self.keys.status(providers.key_env_for(name)) for name in providers.PROVIDERS}
        return {
            "config": config.model_dump(mode="json"),
            "config_path": str(self.config_path()),
            "config_found": str(found) if found else None,
            "providers": providers.PROVIDERS,
            "keys": envs,
            "keyring": self.keys.keyring_available(),
            "limits": self.limits,
            "default": {"provider": provider, "model": config.llm.model, "base_url": config.llm.base_url},
        }

    def save_settings(self, body: dict) -> dict:
        path = self.config_path()
        raw: dict[str, Any] = {}
        if path.is_file():
            try:
                raw = tomllib.loads(path.read_text("utf-8"))
            except (OSError, tomllib.TOMLDecodeError) as e:
                raise ValueError(f"could not read {path}: {e}") from e
        llm_in = body.get("llm") or {}
        llm = dict(raw.get("llm") or {})
        for k in ("provider", "model", "base_url", "effort"):
            if k in llm_in:
                v = llm_in[k]
                if v in (None, ""):
                    llm.pop(k, None)
                else:
                    llm[k] = v
        provider = llm.get("provider", "anthropic")
        if llm.get("base_url"):
            llm["base_url"] = providers.clean_base_url(provider, llm["base_url"])
            if not llm["base_url"]:
                llm.pop("base_url")
        env = providers.key_env_for(provider)
        if env == providers.GENERIC_KEY_ENV and os.environ.get(env):
            llm["api_key_env"] = env
        elif llm.get("api_key_env") == providers.GENERIC_KEY_ENV:
            llm.pop("api_key_env")
        raw["llm"] = llm
        if "diagrams_model" in body:
            stages = dict(raw.get("stages") or {})
            diag = dict(stages.get("diagrams") or {})
            if body["diagrams_model"]:
                diag["model"] = body["diagrams_model"]
            else:
                diag.pop("model", None)
            if diag:
                stages["diagrams"] = diag
            else:
                stages.pop("diagrams", None)
            raw["stages"] = stages
        if body.get("pipeline"):
            raw["pipeline"] = {**(raw.get("pipeline") or {}), **body["pipeline"]}
        if body.get("tts"):
            raw["tts"] = {**(raw.get("tts") or {}), **body["tts"]}
        Config.model_validate(raw)  # refuse to write a file the CLI could not read
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# PaperMap configuration (written by papermap ui; API keys are never stored here).\n\n" + dump_toml(raw), "utf-8")
        if body.get("limits"):
            for lane in ("local", "cloud"):
                if lane in body["limits"]:
                    self.limits[lane] = max(1, min(16, int(body["limits"][lane])))
            ui = self._load_ui()
            ui["limits"] = self.limits
            _atomic_write(self.ui_path, json.dumps(ui).encode("utf-8"))
            self.jobs.wake.set()
        return self.settings()

    def run_config(self, provider: str, model: str, base_url: str | None, options: dict) -> dict:
        config, _ = load_config(None)
        data = config.model_dump(mode="json")
        same = provider == config.llm.provider
        llm = data["llm"]
        llm.update(provider=provider, model=model)
        llm["base_url"] = providers.clean_base_url(provider, base_url) or (llm.get("base_url") if same else None)
        env = providers.key_env_for(provider)
        llm["api_key_env"] = env if env == providers.GENERIC_KEY_ENV and os.environ.get(env) else (llm.get("api_key_env") if same else None)
        if not same:
            data["stages"] = {}
        pipe = data["pipeline"]
        if options.get("style") in ("narrator", "duo"):
            pipe["narration_style"] = options["style"]
        for k in ("quiz", "review", "use_latex"):
            if k in options:
                pipe[k] = bool(options[k])
        if options.get("tts") in ("browser", "none", "edge", "openai"):
            data["tts"]["provider"] = options["tts"]
        return Config.model_validate(data).model_dump(mode="json")

    # --------------------------------------------------------------- Q&A
    def engine(self, name: str) -> tuple[Any, str]:
        from ..llm import LLMClient, create_provider
        from ..qa import QAEngine

        d = self.library.path(name)
        if d is None:
            return None, "no such project"
        exp = d / "experience.json"
        mtime = exp.stat().st_mtime if exp.is_file() else 0
        with self.engine_lock:
            hit = self.engines.get(name)
            if hit and hit[0] == mtime and hit[1] is not None:
                return hit[1], hit[2]
            try:
                saved = d / "papermap.config.json"
                config = Config.model_validate(json.loads(saved.read_text("utf-8"))) if saved.is_file() else load_config(None)[0]
                eng = QAEngine(d, LLMClient(create_provider(config.llm_for("qa")), Cache(self.cache_dir)))
                reason = ""
            except Exception as e:  # missing key, missing index, unknown provider: say why on the page
                eng, reason = None, str(e)
            self.engines[name] = (mtime, eng, reason)
            return eng, reason

    # ------------------------------------------------------------ profiles
    def profiles(self) -> list[dict]:
        rows = []
        names = self.library.names()
        if self.profiles_dir.is_dir():
            for f in sorted(self.profiles_dir.glob("*.md")):
                # output folders are named <paper>-<profile>[--<model>] (see cli.default_out_dir)
                mine = re.compile(rf"-{re.escape(f.stem)}(--|$)")
                papers = sum(1 for n in names if mine.search(n))
                rows.append({"name": f.stem, "text": f.read_text("utf-8"), "papers": papers})
        return rows

    def profile_path(self, name: str) -> Path | None:
        if not NAME_RE.match(name or "") or name.startswith("."):
            return None
        return self.profiles_dir / f"{name}.md"


# -------------------------------------------------------------------- handler


class Handler(BaseHTTPRequestHandler):
    app: App

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    # ---------------------------------------------------------- responses
    def _send(self, status: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _err(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _redirect(self, location: str, cookie: str | None = None) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _file(self, path: Path, download: str | None = None) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            return self._err(404, "not found")
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        headers = {"Content-Disposition": f"attachment; filename=\"{download}\""} if download else None
        self._send(200, body, ctype, headers)

    # ---------------------------------------------------------- security
    def _guard(self) -> bool:
        """Host check (DNS rebinding), origin check (cross-site writes) and token."""
        app = self.app
        if (self.headers.get("Host") or "") not in app.allowed_hosts():
            self._send(421, b"wrong host", "text/plain; charset=utf-8")
            return False
        if self.command not in ("GET", "HEAD"):
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc not in app.allowed_hosts():
                self._err(403, "cross-origin request refused")
                return False
        url = urlsplit(self.path)
        given = parse_qs(url.query).get("token", [""])[0]
        if given and secrets.compare_digest(given, app.token) and self.command == "GET":
            rest = "&".join(p for p in url.query.split("&") if not p.startswith("token="))
            cookie = f"{COOKIE}={app.token}; Path=/; HttpOnly; SameSite=Strict"
            self._redirect(url.path + (f"?{rest}" if rest else ""), cookie)
            return False
        jar = SimpleCookie(self.headers.get("Cookie") or "")
        if COOKIE in jar and secrets.compare_digest(jar[COOKIE].value, app.token):
            return True
        page = (
            "<!doctype html><meta charset=utf-8><title>PaperMap</title>"
            "<body style='font:15px system-ui;padding:40px;max-width:560px'>"
            "<h2>Open the link printed by <code>papermap ui</code></h2>"
            "<p>This dashboard only opens from the link shown in the terminal where you started it, "
            "so other websites cannot use it. Run <code>papermap ui --print-url</code> to see it again.</p>"
        ).encode()
        self._send(401, page, "text/html; charset=utf-8")
        return False

    # ------------------------------------------------------------ routing
    def do_HEAD(self):  # noqa: N802
        self.do_GET()

    def do_GET(self):  # noqa: N802
        if not self._guard():
            return
        try:
            self._get(urlsplit(self.path))
        except BrokenPipeError:
            pass
        except Exception as e:
            log.exception("GET %s failed", self.path)
            self._err(500, str(e))

    def do_POST(self):  # noqa: N802
        if not self._guard():
            return
        try:
            self._post(urlsplit(self.path))
        except BrokenPipeError:
            pass
        except ValueError as e:
            self._err(400, str(e))
        except Exception as e:
            log.exception("POST %s failed", self.path)
            self._err(500, str(e))

    do_PUT = do_POST  # noqa: N815

    def _get(self, url) -> None:
        app = self.app
        path = url.path
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if path in ("/", "/index.html"):
            return self._send(200, _asset("index.html"), "text/html; charset=utf-8")
        if path in ("/ui.js", "/ui.css"):
            return self._send(200, _asset(path[1:]), "text/javascript; charset=utf-8" if path.endswith(".js") else "text/css; charset=utf-8")
        if path.startswith("/p/"):
            return self._project_get(path)
        if path == "/api/state":
            return self._json(200, {"version": __version__, "workspace": str(app.workspace), "library": str(app.library.root),
                                    "profiles_dir": str(app.profiles_dir), "cache": str(app.cache_dir)})
        if path == "/api/projects":
            return self._json(200, app.library.all())
        m = re.fullmatch(r"/api/projects/([^/]+)", path)
        if m:
            s = app.library.summary(m.group(1), with_concepts=True)
            return self._json(200, s) if s else self._err(404, "no such project")
        m = re.fullmatch(r"/api/projects/([^/]+)/export\.(zip|html)", path)
        if m:
            name, kind = m.groups()
            if app.library.path(name) is None:
                return self._err(404, "no such project")
            if kind == "zip":
                return self._send(200, app.library.zip_bytes(name), "application/zip", {"Content-Disposition": f'attachment; filename="{name}.zip"'})
            return self._send(200, app.library.single_html(name).encode("utf-8"), "text/html; charset=utf-8",
                              {"Content-Disposition": f'attachment; filename="{name}.html"'})
        if path == "/api/search":
            return self._json(200, app.library.search(q.get("q", "")))
        if path == "/api/connections":
            return self._json(200, app.library.connections())
        if path == "/api/runs":
            return self._json(200, app.jobs.view())
        m = re.fullmatch(r"/api/runs/([0-9a-f]+)/log", path)
        if m:
            return self._json(200, {"lines": app.jobs.log_lines(m.group(1))})
        if path == "/api/settings":
            return self._json(200, app.settings())
        if path == "/api/models":
            models, error = providers.list_models(q.get("provider", ""), q.get("base_url") or None)
            return self._json(200, {"models": models, "error": error, "tested": sorted(providers.TESTED)})
        if path == "/api/estimate":
            pages = int(q["pages"]) if q.get("pages", "").isdigit() else None
            return self._json(200, providers.estimate(q.get("provider", ""), q.get("model", ""), q.get("base_url") or None, pages))
        if path == "/api/profiles":
            return self._json(200, {"profiles": app.profiles(), "template": EXAMPLE_PROFILE, "dir": str(app.profiles_dir)})
        if path == "/api/cache":
            return self._json(200, app.cache.overview())
        return self._err(404, "not found")

    def _post(self, url) -> None:
        app = self.app
        path = url.path
        if path.startswith("/p/"):
            return self._project_post(path)
        if path == "/api/upload":
            return self._upload()
        body, err = read_json(self, limit=2 * 1024 * 1024)
        if body is None and int(self.headers.get("Content-Length") or 0) > 0:
            return self._err(400, err)
        body = body or {}

        if path == "/api/settings":
            return self._json(200, app.save_settings(body))
        if path == "/api/keys":
            env = body.get("env")
            if env not in {*providers.KEY_ENV.values(), providers.GENERIC_KEY_ENV}:
                return self._err(400, "unknown key")
            warning = app.keys.set(env, str(body.get("value", "")), bool(body.get("remember")))
            app.engines.clear()
            return self._json(200, {"warning": warning, "keys": app.settings()["keys"]})
        if path == "/api/keys/forget":
            env = body.get("env")
            if env not in {*providers.KEY_ENV.values(), providers.GENERIC_KEY_ENV}:
                return self._err(400, "unknown key")
            app.keys.forget(env)
            app.engines.clear()
            return self._json(200, {"keys": app.settings()["keys"]})
        if path == "/api/check":
            base = load_config(None)[0].llm
            settings = LLMSettings.model_validate({
                **base.model_dump(), "provider": body.get("provider") or base.provider, "model": body.get("model") or base.model,
                "base_url": providers.clean_base_url(body.get("provider", ""), body.get("base_url")),
                "api_key_env": providers.GENERIC_KEY_ENV if providers.key_env_for(body.get("provider", "")) == providers.GENERIC_KEY_ENV
                and os.environ.get(providers.GENERIC_KEY_ENV) else None,
            })
            return self._json(200, providers.test_connection(settings))
        if path == "/api/runs":
            return self._submit(body)
        if path == "/api/runs/clear":
            return self._json(200, {"removed": app.jobs.clear_finished()})
        m = re.fullmatch(r"/api/runs/([0-9a-f]+)/(cancel|resume|remove)", path)
        if m:
            job_id, action = m.groups()
            ok = getattr(app.jobs, action)(job_id)
            return self._json(200, {"ok": ok}) if ok else self._err(409, f"cannot {action} this run now")
        if path == "/api/rerender-all":
            return self._rerender(app.library.names())
        m = re.fullmatch(r"/api/projects/([^/]+)/(rerender|reveal|delete)", path)
        if m:
            name, action = m.groups()
            d = app.library.path(name)
            if d is None:
                return self._err(404, "no such project")
            if action == "rerender":
                return self._rerender([name])
            if action == "reveal":
                opener = {"darwin": ["open"], "win32": ["explorer"]}.get(sys.platform, ["xdg-open"])
                subprocess.Popen([*opener, str(d)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return self._json(200, {"ok": True, "path": str(d)})
            if any(j["out"] == name and j["state"] in ("running", "queued") for j in app.jobs.view()):
                return self._err(409, "cancel the run for this paper first")
            app.engines.pop(name, None)
            return self._json(200, {"ok": app.library.delete(name)})
        m = re.fullmatch(r"/api/profiles/([^/]+)(/delete)?", path)
        if m:
            p = app.profile_path(m.group(1))
            if p is None:
                return self._err(400, "use letters, digits, dots, dashes or underscores for the name")
            if m.group(2):
                p.unlink(missing_ok=True)
            else:
                text = str(body.get("text", ""))
                if not text.strip():
                    return self._err(400, "the profile is empty")
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text, "utf-8")
            return self._json(200, {"profiles": app.profiles()})
        if path == "/api/cache/clear":
            if app.jobs.busy():
                return self._err(409, "wait until no run is in progress")
            if body.get("project"):
                freed = app.cache.clear_project(str(body["project"]))
            elif body.get("kind"):
                freed = app.cache.clear_kind(str(body["kind"]))
            else:
                return self._err(400, "say what to clear")
            return self._json(200, {"freed": freed, **app.cache.overview()})
        return self._err(404, "not found")

    # ------------------------------------------------------------ actions
    def _rerender(self, names: list[str]) -> None:
        from ..stages.render import rerender

        done, failed = [], []
        for n in names:
            d = self.app.library.path(n)
            if d is None or not (d / "experience.json").is_file():
                continue
            try:
                rerender(d, f"papermap {__version__}")
                done.append(n)
            except Exception as e:  # one broken folder must not stop the others
                failed.append({"name": n, "error": str(e)})
        self._json(200, {"done": done, "failed": failed})

    def _submit(self, body: dict) -> None:
        app = self.app
        sources = [s.strip() for s in body.get("sources") or [] if isinstance(s, str) and s.strip()]
        uploads = [u for u in body.get("uploads") or [] if isinstance(u, dict)]
        items: list[tuple[str, str, int | None]] = [(s, s.rstrip("/").split("/")[-1], None) for s in sources]
        for u in uploads:
            p = (app.uploads_dir / str(u.get("file", ""))).resolve()
            if not p.is_relative_to(app.uploads_dir.resolve()) or not p.is_file():
                return self._err(400, f"upload not found: {u.get('name')}")
            items.append((str(p), str(u.get("name") or p.name), u.get("pages")))
        if not items:
            return self._err(400, "add at least one arXiv link or PDF")
        models: list[dict] = []
        for m in body.get("models") or []:
            if isinstance(m, dict) and m.get("provider") and m.get("model") and not any(
                (x["provider"], x["model"], x.get("base_url")) == (m["provider"], m["model"], m.get("base_url")) for x in models
            ):
                models.append(m)
        if not models:
            return self._err(400, "choose a model")
        for m in models:
            env = providers.key_env_for(m["provider"])
            if m["provider"] in ("openai", "openrouter") and not os.environ.get(env or ""):
                label = providers.PROVIDERS[m["provider"]]["label"]
                return self._err(400, f"add your {label} API key in Model settings first")
        profile = body.get("profile") or None
        profile_path = None
        if profile:
            p = app.profile_path(profile)
            if p is None or not p.is_file():
                return self._err(400, f"profile not found: {profile}")
            profile_path = str(p)
        options = body.get("options") or {}
        created = []
        for source, label, pages in items:
            for m in models:
                config = app.run_config(m["provider"], m["model"], m.get("base_url"), options)
                created.append(app.jobs.submit(source=source, label=label, profile=profile, profile_path=profile_path,
                                               config=config, compare=len(models) > 1, pages=pages))
        self._json(200, {"queued": [j["id"] for j in created], "runs": app.jobs.view()})

    def _upload(self) -> None:
        app = self.app
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_UPLOAD:
            return self._err(413, "the PDF must be under 200 MB")
        name = Path(self.headers.get("X-Filename") or "paper.pdf").name
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(name).stem).strip("-.")[:80] or "paper"
        data = self.rfile.read(length)
        if not data.startswith(b"%PDF"):
            return self._err(400, f"{name} is not a PDF")
        app.uploads_dir.mkdir(parents=True, exist_ok=True)
        target = app.uploads_dir / f"{stem}.pdf"
        k = 2
        while target.exists() and target.read_bytes() != data:
            target = app.uploads_dir / f"{stem}-{k}.pdf"
            k += 1
        target.write_bytes(data)
        pages = None
        try:
            import pymupdf

            with pymupdf.open(target) as doc:
                pages = doc.page_count
        except Exception:
            pass
        self._json(200, {"file": target.name, "name": name, "pages": pages})

    # ------------------------------------------------------ project pages
    def _project_parts(self, path: str) -> tuple[str, str] | None:
        m = re.fullmatch(r"/p/([^/]+)(/.*)?", path)
        if not m:
            return None
        return m.group(1), (m.group(2) or "")

    def _project_get(self, path: str) -> None:
        parts = self._project_parts(path)
        if not parts:
            return self._err(404, "not found")
        name, rest = parts
        d = self.app.library.path(name)
        if d is None:
            return self._err(404, "no such project")
        if rest == "":
            return self._redirect(f"/p/{quote(name)}/")
        if rest == "/api/health":
            eng, reason = self.app.engine(name)
            info = {"provider": eng.llm.provider.name, "model": eng.llm.settings.model} if eng else {"reason": reason}
            return self._json(200, {"ok": eng is not None, **info})
        rel = rest.lstrip("/") or "index.html"
        target = (d / rel).resolve()
        if not target.is_relative_to(d.resolve()) or not target.is_file():
            return self._err(404, "not found")
        if target.relative_to(d.resolve()).parts[0] in ("logs", "debug") or target.name in ("papermap.config.json",):
            return self._err(404, "not found")
        self._file(target)

    def _project_post(self, path: str) -> None:
        parts = self._project_parts(path)
        if not parts:
            return self._err(404, "not found")
        name, rest = parts
        d = self.app.library.path(name)
        route = rest.rsplit("/api/", 1)[-1] if "/api/" in rest else ""
        if d is None or route not in ("ask", "quiz", "progress"):
            return self._err(404, "not found")
        req, err = read_json(self)
        if req is None:
            return self._err(400, err)
        if route == "ask":
            eng, reason = self.app.engine(name)
            if eng is None:
                return self._err(503, reason or "Q&A is not configured")
            return self._json(*answer(eng, req))
        return self._json(*record_activity(d, route, req))


# ----------------------------------------------------------------------- run


def run_ui(workspace: Path, host: str = "127.0.0.1", port: int = 8770, open_browser: bool = True) -> int:
    workspace = Path(workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    os.chdir(workspace)  # runs resolve ./papermap.toml and relative paths like the CLI does here
    server = None
    for p in range(port, port + 20):
        try:
            server = ThreadingHTTPServer((host, p), Handler)
            port = p
            break
        except OSError:
            continue
    if server is None:
        raise OSError(f"no free port in {port}-{port + 19}")
    server.daemon_threads = True
    app = App(workspace, port, host)
    Handler.app = app
    shown = "127.0.0.1" if host in ("0.0.0.0", "127.0.0.1") else host
    url = f"http://{shown}:{port}/?token={app.token}"
    _atomic_write(app.state_dir / "url", url.encode())
    log.info("PaperMap dashboard for %s", workspace)
    log.info("open %s", url)
    log.info("library: %s | Ctrl+C stops the dashboard; runs in progress keep going", app.library.root)
    if host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("listening on %s: anyone on your network with the link can spend your API credits", host)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("stopped")
    finally:
        server.server_close()
    return 0


def print_url(workspace: Path) -> str | None:
    """The link of the dashboard last started in this workspace (it carries the access token)."""
    path = Path(workspace).resolve() / ".papermap-ui" / "url"
    return path.read_text().strip() if path.is_file() else None


__all__ = ["print_url", "run_ui"]
