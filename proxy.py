"""Filtered Docker state API. This process alone holds the Docker socket."""
from __future__ import annotations

import http.client
import json
import os
import re
import signal
import socket
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Mapping
from health_contract import sanitize_health

SOCKET_PATH = "/var/run/docker.sock"
MAX_CONTAINERS = 512


@dataclass(frozen=True)
class ProxySettings:
    self_name: str
    include_self: bool

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ProxySettings:
        values = os.environ if env is None else env
        name = values.get("SELF_CONTAINER_NAME", "status-docker-proxy")
        include = values.get("INCLUDE_SELF", "false").lower()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", name):
            raise ValueError("Invalid SELF_CONTAINER_NAME")
        if include not in {"true", "false"}:
            raise ValueError("INCLUDE_SELF must be true or false")
        return cls(name, include == "true")


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str):
        super().__init__("docker", timeout=5)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect(self.socket_path)


class DockerSocketClient:
    """Only fixed list and inspect GET paths are ever sent to Docker."""

    def __init__(self, socket_path: str = SOCKET_PATH):
        self.socket_path = socket_path

    def get(self, path: str, limit: int) -> object:
        connection = UnixConnection(self.socket_path)
        try:
            connection.request("GET", path, headers={"Host": "docker"})
            response = connection.getresponse()
            if response.status != 200:
                raise OSError("Docker API unavailable")
            raw = response.read(limit + 1)
            if len(raw) > limit:
                raise OSError("Docker API response too large")
            return json.loads(raw)
        finally:
            connection.close()


def timestamp(value: object) -> str | None:
    if not isinstance(value, str) or not value or value.startswith("0001-"):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return None


def sanitized_container(inspect: dict) -> dict:
    """Return only fields permitted to cross the proxy boundary."""
    state = inspect.get("State") or {}
    name = str(inspect.get("Name") or "").lstrip("/")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", name):
        raise ValueError("Invalid container name")
    raw_state = str(state.get("Status") or "unknown")
    status = raw_state if raw_state in {"created", "running", "paused", "restarting", "removing", "exited", "dead"} else "unknown"
    health_obj = state.get("Health")
    raw_health = str(health_obj.get("Status") or "unknown") if isinstance(health_obj, dict) else "none"
    health = raw_health if raw_health in {"healthy", "unhealthy", "starting", "none"} else "unknown"
    since_field = "StartedAt" if status == "running" else "FinishedAt" if status in {"exited", "dead"} else None
    item = {"name": name, "state": status, "health": health,
            "state_since": timestamp(state.get(since_field)) if since_field else None}
    logs = health_obj.get("Log", []) if isinstance(health_obj, dict) else []
    if isinstance(logs, list) and logs and isinstance(logs[-1], dict):
        output = logs[-1].get("Output")
        if isinstance(output, str) and len(output.encode('utf-8')) <= 4096:
            try:
                detail = sanitize_health(json.loads(output))
            except (ValueError, TypeError):
                detail = None
            if detail:
                item["application_health"] = detail
    return item


class ProxyService:
    def __init__(self, client: DockerSocketClient, settings: ProxySettings):
        self.client = client
        self.settings = settings

    def list_containers(self) -> list[dict]:
        summaries = self.client.get("/containers/json?all=1", 4_000_000)
        if not isinstance(summaries, list) or len(summaries) > MAX_CONTAINERS:
            raise OSError("Invalid Docker container list")
        result = []
        for summary in summaries:
            cid = summary.get("Id") if isinstance(summary, dict) else None
            if not isinstance(cid, str) or not re.fullmatch(r"[0-9a-f]{64}", cid):
                continue
            inspect = self.client.get(f"/containers/{cid}/json", 2_000_000)
            try:
                item = sanitized_container(inspect)
            except (AttributeError, TypeError, ValueError):
                continue
            if self.settings.include_self or item["name"] != self.settings.self_name:
                result.append(item)
        return sorted(result, key=lambda item: item["name"].casefold())


class ProxyHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, service: ProxyService, address: tuple[str, int] = ("0.0.0.0", 8090)):
        self.service = service
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server: ProxyHTTPServer
    server_version = "DockerStateProxy/1.0"
    sys_version = ""

    def setup(self) -> None:
        self.request.settimeout(10)
        super().setup()

    def log_message(self, fmt: str, *args) -> None:
        # No Docker responses, paths, or container identifiers in logs.
        pass

    def reply(self, code: int, body: dict) -> None:
        raw = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            return self.reply(200, {"ok": True})
        if self.path != "/containers":
            return self.reply(404, {"error": "Not found"})
        try:
            self.reply(200, {"containers": self.server.service.list_containers()})
        except (OSError, ValueError, TypeError, json.JSONDecodeError, http.client.HTTPException):
            self.reply(503, {"error": "Docker state unavailable"})

    def do_POST(self) -> None:
        self.reply(405, {"error": "Method not allowed"})

    do_PUT = do_DELETE = do_PATCH = do_POST


def main() -> None:
    try:
        settings = ProxySettings.from_env()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    server = ProxyHTTPServer(ProxyService(DockerSocketClient(), settings))

    def stop_server(*_args) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
