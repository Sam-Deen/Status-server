import http.cookiejar
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

from status_server.auth import AuthService
from status_server.checks import CheckerRegistry, DockerChecker, DockerUnavailable, HttpChecker, TcpChecker
from status_server.config import Settings
from status_server.models import DockerContainer
from status_server.repository import Database, MonitorRepository, SessionRepository
from status_server.service import MonitorService
from status_server.web import StatusHTTPServer


class FakeDockerClient:
    available = True

    def list_containers(self):
        if not self.available:
            raise DockerUnavailable
        return [DockerContainer("background-worker", "running", "none", None)]


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = replace(Settings.from_env({"ADMIN_PASSWORD": "unit-test-only-password-123456",
                                                   "COOKIE_SECURE": "false"}),
                                db_path=str(Path(self.tmp.name) / "status.db"), host="127.0.0.1", port=0)
        database = Database(self.settings.db_path)
        database.initialize()
        client = FakeDockerClient()
        checker = DockerChecker(client)
        registry = CheckerRegistry([HttpChecker(), TcpChecker(), checker])
        service = MonitorService(MonitorRepository(database), registry, checker)
        auth = AuthService(self.settings, SessionRepository(database))
        self.server = StatusHTTPServer(self.settings, service, auth, database,
                                       Path(__file__).resolve().parents[1] / "static")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.docker_client = client

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(self, path, method="GET", data=None, csrf=None):
        headers = {"Content-Type": "application/json"} if data is not None else {}
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
        request = urllib.request.Request(self.base + path, method=method, headers=headers,
                                         data=json.dumps(data).encode() if data is not None else None)
        try:
            with self.opener.open(request, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_routes_auth_csrf_public_allowlist_and_static(self):
        for path in ("/", "/admin", "/app.js", "/style.css"):
            with self.opener.open(self.base + path, timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertGreater(len(response.read()), 100)
        self.assertEqual(self.request("/healthz"), (200, {"ok": True}))
        self.assertEqual(self.request("/api/admin/monitors")[0], 401)
        self.assertEqual(self.request("/api/admin/containers")[0], 401)
        self.assertEqual(self.request("/api/admin/login", "POST", {"username": "admin", "password": "wrong"})[0], 401)
        status, login = self.request("/api/admin/login", "POST",
                                     {"username": "admin", "password": self.settings.admin_password})
        self.assertEqual(status, 200)
        csrf = login["csrf"]
        monitor = {"name": "Background Worker", "description": "", "type": "docker",
                   "target": "background-worker", "interval": 60, "timeout": 5, "threshold": 1, "enabled": True}
        self.assertEqual(self.request("/api/admin/monitors", "POST", monitor)[0], 401)
        status, created = self.request("/api/admin/monitors", "POST", monitor, csrf)
        self.assertEqual(status, 201)
        self.assertEqual(self.request("/api/admin/containers")[0], 200)
        public = self.request("/api/status")[1]["monitors"][0]
        self.assertEqual(public["container_name"], "background-worker")
        self.assertNotIn("target", public)
        self.assertNotIn("container_id", public)
        self.assertEqual(self.request("/api/admin/monitors")[1]["monitors"][0]["target"], "background-worker")
        self.assertEqual(self.request(f"/api/admin/monitors/{created['id']}", "DELETE", csrf=csrf)[0], 200)
        self.assertEqual(self.request("/api/admin/logout", "POST", csrf=csrf)[0], 200)
        self.assertEqual(self.request("/api/admin/monitors")[0], 401)

    def test_docker_outage_does_not_break_status_api(self):
        csrf = self.request("/api/admin/login", "POST",
                            {"username": "admin", "password": self.settings.admin_password})[1]["csrf"]
        self.docker_client.available = False
        self.assertEqual(self.request("/api/admin/containers")[0], 503)
        self.assertEqual(self.request("/api/status")[0], 200)
        self.assertEqual(self.request("/healthz")[0], 200)


if __name__ == "__main__":
    unittest.main()
