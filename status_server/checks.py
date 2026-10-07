"""Monitor transports and the restricted Docker proxy client."""
from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.request
from collections import defaultdict
from urllib.parse import urlsplit

from .models import CheckResult, DockerContainer, Monitor


class InvalidTarget(ValueError):
    pass


class DockerUnavailable(Exception):
    pass


class Checker:
    kinds: tuple[str, ...] = ()

    def validate_target(self, kind: str, target: str) -> None:
        raise NotImplementedError

    def ensure_selection(self, target: str, unchanged: bool = False) -> None:
        pass

    def check(self, monitor: Monitor) -> CheckResult:
        raise NotImplementedError

    def check_many(self, monitors: list[Monitor]) -> dict[int, CheckResult]:
        return {monitor.id: self.check(monitor) for monitor in monitors}


class HttpChecker(Checker):
    kinds = ("http", "https")

    def validate_target(self, kind: str, target: str) -> None:
        try:
            parts = urlsplit(target)
            valid = (parts.scheme == kind and bool(parts.hostname) and not parts.username
                     and not parts.password and not parts.fragment
                     and not any(char.isspace() for char in target))
            port = parts.port
        except ValueError:
            raise InvalidTarget("Invalid URL") from None
        if not valid:
            raise InvalidTarget("Use a full URL matching the selected type, without credentials or a fragment")
        if port is not None and not 1 <= port <= 65535:
            raise InvalidTarget("Invalid URL port")

    def check(self, monitor: Monitor) -> CheckResult:
        started = time.monotonic()
        try:
            parts = urlsplit(monitor.target)
            connection_type = http.client.HTTPSConnection if monitor.type == "https" else http.client.HTTPConnection
            connection = connection_type(parts.hostname, parts.port, timeout=monitor.timeout)
            try:
                path = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
                connection.request("GET", path, headers={"User-Agent": "StatusServer/1.0", "Accept": "*/*"})
                response = connection.getresponse()
                healthy = 200 <= response.status < 400
                response.read(1024)
            finally:
                connection.close()
            return CheckResult(healthy, round((time.monotonic() - started) * 1000))
        except (OSError, TimeoutError, ValueError, UnicodeError, http.client.HTTPException):
            return CheckResult(False)


class TcpChecker(Checker):
    kinds = ("tcp",)

    def validate_target(self, kind: str, target: str) -> None:
        match = re.fullmatch(r"(?:\[([0-9a-fA-F:]+)\]|([^:\s/]+)):(\d{1,5})", target)
        if not match or not 1 <= int(match.group(3)) <= 65535:
            raise InvalidTarget("TCP target must be host:port or [IPv6]:port")
        host = match.group(1) or match.group(2)
        if match.group(1):
            try:
                ipaddress.IPv6Address(host)
            except ValueError:
                raise InvalidTarget("Invalid IPv6 address") from None
        elif not re.fullmatch(r"[A-Za-z0-9_.-]+", host):
            raise InvalidTarget("Invalid TCP host")

    def check(self, monitor: Monitor) -> CheckResult:
        started = time.monotonic()
        host, port = monitor.target.rsplit(":", 1)
        try:
            with socket.create_connection((host.strip("[]"), int(port)), timeout=monitor.timeout):
                pass
            return CheckResult(True, round((time.monotonic() - started) * 1000))
        except (OSError, TimeoutError, ValueError):
            return CheckResult(False)


class DockerProxyClient:
    """Only reads the proxy's four-field response, never the Docker socket."""

    VALID_STATES = {"created", "running", "paused", "restarting", "removing", "exited", "dead", "unknown"}
    VALID_HEALTH = {"healthy", "unhealthy", "starting", "none", "unknown"}

    def __init__(self, url: str):
        self.url = url
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def list_containers(self) -> list[DockerContainer]:
        try:
            request = urllib.request.Request(self.url, headers={"Accept": "application/json"})
            with self.opener.open(request, timeout=5) as response:
                raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError
            items = json.loads(raw)["containers"]
            if not isinstance(items, list) or len(items) > 512:
                raise ValueError
            result = []
            for item in items:
                name, state, health = item["name"], item["state"], item["health"]
                since = item.get("state_since")
                if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", name)
                        or state not in self.VALID_STATES or health not in self.VALID_HEALTH
                        or (since is not None and (not isinstance(since, str) or len(since) > 64))):
                    raise ValueError
                result.append(DockerContainer(name, state, health, since))
            return result
        except (OSError, TimeoutError, ValueError, KeyError, TypeError, AttributeError, UnicodeError,
                urllib.error.URLError, json.JSONDecodeError, http.client.HTTPException) as exc:
            raise DockerUnavailable("Docker discovery is unavailable") from exc


class DockerChecker(Checker):
    kinds = ("docker",)

    def __init__(self, client: DockerProxyClient):
        self.client = client

    def validate_target(self, kind: str, target: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", target):
            raise InvalidTarget("Select a valid Docker container name")

    def ensure_selection(self, target: str, unchanged: bool = False) -> None:
        if unchanged:
            return
        if target not in {container.name for container in self.client.list_containers()}:
            raise InvalidTarget("Select a container from the discovered list")

    @staticmethod
    def state_result(container: DockerContainer) -> CheckResult:
        if container.state == "running":
            status = {"healthy": "operational", "none": "operational", "starting": "degraded",
                      "unhealthy": "down"}.get(container.health, "unknown")
        else:
            status = {"restarting": "degraded", "paused": "down", "created": "down",
                      "removing": "down", "exited": "down", "dead": "down"}.get(container.state, "unknown")
        return CheckResult(status == "operational" if status != "unknown" else None,
                           status=status, docker=container)

    def check_many(self, monitors: list[Monitor]) -> dict[int, CheckResult]:
        try:
            containers = {container.name: container for container in self.client.list_containers()}
        except DockerUnavailable:
            return {monitor.id: CheckResult(None, status="unknown",
                    docker=DockerContainer(monitor.target, "unknown", "unknown", None)) for monitor in monitors}
        result = {}
        for monitor in monitors:
            container = containers.get(monitor.target, DockerContainer(monitor.target, "missing", "none", None))
            result[monitor.id] = self.state_result(container)
        return result


class CheckerRegistry:
    def __init__(self, checkers: list[Checker]):
        self._by_type = {kind: checker for checker in checkers for kind in checker.kinds}

    def for_type(self, kind: str) -> Checker | None:
        return self._by_type.get(kind)

    def groups(self, monitors: list[Monitor]) -> dict[Checker, list[Monitor]]:
        grouped: dict[Checker, list[Monitor]] = defaultdict(list)
        for monitor in monitors:
            checker = self.for_type(monitor.type)
            if checker is not None:
                grouped[checker].append(monitor)
        return grouped
