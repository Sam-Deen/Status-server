"""HTTP boundary: request parsing, authentication, and response formatting."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .auth import AuthService, InvalidCredentials, Session
from .checks import DockerUnavailable, InvalidTarget
from .config import Settings
from .repository import Database
from .service import MonitorNotFound, MonitorService
from .validation import InvalidMonitor

LOG = logging.getLogger(__name__)
MONITOR_PATH = re.compile(r"/api/admin/monitors/(\d+)")
HISTORY_PATH = re.compile(r"/api/admin/monitors/(\d+)/history")


class AuthenticationRequired(Exception):
    pass


class InvalidBody(Exception):
    pass


class StatusHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, settings: Settings, service: MonitorService, auth: AuthService,
                 database: Database, static_root: Path):
        self.settings = settings
        self.service = service
        self.auth = auth
        self.database = database
        self.static_root = static_root
        super().__init__((settings.host, settings.port), RequestHandler)


class RequestHandler(BaseHTTPRequestHandler):
    server: StatusHTTPServer
    server_version = "Status/1.0"
    sys_version = ""

    def setup(self) -> None:
        self.request.settimeout(10)
        super().setup()

    def log_message(self, fmt: str, *args) -> None:
        pass

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        LOG.info("http %s %s %s", self.command, self.path.split("?", 1)[0], code)

    def _send_bytes(self, status: int, data: bytes, content_type: str, cookie: str | None = None,
                    cache_control: str = "no-store") -> None:
        self.send_response(status)
        for name, value in (
            ("Content-Type", content_type), ("Content-Length", str(len(data))),
            ("Cache-Control", cache_control), ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"), ("X-Frame-Options", "DENY"),
            ("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; "
             "img-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"),
        ):
            self.send_header(name, value)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_json(self, status: int, payload: dict, cookie: str | None = None) -> None:
        self._send_bytes(status, json.dumps(payload, separators=(",", ":")).encode(),
                         "application/json; charset=utf-8", cookie)

    def send_static(self, name: str, content_type: str) -> None:
        data = (self.server.static_root / name).read_bytes()
        self._send_bytes(200, data, content_type, cache_control="no-cache")

    def read_json(self) -> object:
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 <= size <= self.server.settings.max_body_bytes:
                raise ValueError
            return json.loads(self.rfile.read(size))
        except (ValueError, json.JSONDecodeError, TimeoutError):
            raise InvalidBody from None

    def session(self, require_csrf: bool = False) -> Session:
        csrf = self.headers.get("X-CSRF-Token", "") if require_csrf else None
        session = self.server.auth.authenticate(self.headers.get("Cookie", ""), csrf)
        if session is None:
            raise AuthenticationRequired
        return session

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        self.dispatch(self.get)

    def do_POST(self) -> None:
        self.dispatch(self.post)

    def do_PUT(self) -> None:
        self.dispatch(self.put)

    def do_DELETE(self) -> None:
        self.dispatch(self.delete)

    def dispatch(self, action) -> None:
        try:
            action(self.path.split("?", 1)[0])
        except (InvalidBody, InvalidMonitor, InvalidTarget) as exc:
            self.send_json(400, {"error": str(exc) if str(exc) else "Invalid JSON body"})
        except (AuthenticationRequired, InvalidCredentials) as exc:
            self.send_json(401, {"error": "Invalid credentials" if isinstance(exc, InvalidCredentials)
                                 else "Authentication required"})
        except MonitorNotFound:
            self.send_json(404, {"error": "Monitor not found"})
        except DockerUnavailable:
            self.send_json(503, {"error": "Docker discovery is unavailable"})
        except sqlite3.Error:
            LOG.error("database request failed")
            self.send_json(503, {"error": "Storage unavailable"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            LOG.error("request failed: %s", type(exc).__name__)
            self.send_json(500, {"error": "Internal server error"})

    def get(self, path: str) -> None:
        if path == "/healthz":
            healthy = self.server.database.healthy()
            return self.send_json(200 if healthy else 503, {"ok": healthy})
        if path == "/api/status":
            return self.send_json(200, self.server.service.public_status())
        if path == "/api/latency":
            return self.send_json(200, self.server.service.public_latency())
        if path == "/api/admin/me":
            session = self.server.auth.authenticate(self.headers.get("Cookie", ""))
            return self.send_json(200, {"authenticated": bool(session), "csrf": session.csrf if session else None})
        if path == "/api/admin/monitors":
            self.session()
            return self.send_json(200, {"monitors": self.server.service.admin_monitors()})
        if path == "/api/admin/containers":
            self.session()
            return self.send_json(200, {"containers": self.server.service.discover_containers()})
        if path == "/api/admin/incidents":
            self.session()
            return self.send_json(200, {"incidents": self.server.service.incidents()})
        if match := HISTORY_PATH.fullmatch(path):
            self.session()
            return self.send_json(200, {"checks": self.server.service.history(int(match.group(1)))})
        static = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/admin": ("index.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8")}
        if path in static:
            return self.send_static(*static[path])
        self.send_json(404, {"error": "Not found"})

    def post(self, path: str) -> None:
        if path == "/api/admin/login":
            token, csrf = self.server.auth.login(self.read_json())
            return self.send_json(200, {"authenticated": True, "csrf": csrf}, self.server.auth.cookie(token))
        session = self.session(require_csrf=True)
        if path == "/api/admin/logout":
            self.server.auth.logout(session)
            return self.send_json(200, {"ok": True}, self.server.auth.cookie("", clear=True))
        if path == "/api/admin/monitors":
            return self.send_json(201, {"id": self.server.service.create(self.read_json())})
        self.send_json(404, {"error": "Not found"})

    def put(self, path: str) -> None:
        self.session(require_csrf=True)
        if match := MONITOR_PATH.fullmatch(path):
            self.server.service.update(int(match.group(1)), self.read_json())
            return self.send_json(200, {"ok": True})
        self.send_json(404, {"error": "Not found"})

    def delete(self, path: str) -> None:
        self.session(require_csrf=True)
        if match := MONITOR_PATH.fullmatch(path):
            deleted = self.server.service.delete(int(match.group(1)))
            return self.send_json(200 if deleted else 404, {"ok": deleted})
        self.send_json(404, {"error": "Not found"})
