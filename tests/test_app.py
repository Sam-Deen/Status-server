import socket
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from status_server.auth import AuthService, InvalidCredentials
from status_server.checks import (CheckerRegistry, DockerChecker, DockerProxyClient, DockerUnavailable,
                                  HttpChecker, InvalidTarget, TcpChecker)
from status_server.config import ConfigurationError, Settings
from status_server.models import CheckResult, DockerContainer, MonitorConfig
from status_server.repository import Database, MonitorRepository, SessionRepository
from status_server.service import MonitorService
from status_server.validation import InvalidMonitor, parse_monitor


class FakeDockerClient:
    def __init__(self):
        self.containers = [DockerContainer("background-worker", "running", "healthy", None)]
        self.available = True

    def list_containers(self):
        if not self.available:
            raise DockerUnavailable("test outage")
        return self.containers


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "status.db")
        self.database = Database(self.path)
        self.database.initialize()
        self.repository = MonitorRepository(self.database)
        self.sessions = SessionRepository(self.database)
        self.docker_client = FakeDockerClient()
        self.docker_checker = DockerChecker(self.docker_client)
        self.registry = CheckerRegistry([HttpChecker(), TcpChecker(), self.docker_checker])
        self.service = MonitorService(self.repository, self.registry, self.docker_checker)

    def tearDown(self):
        self.tmp.cleanup()

    def config(self, **updates):
        data = dict(name="Web Application", description="", type="http", target="http://localhost:8080/health",
                    interval=60, timeout=5, threshold=2, enabled=True)
        data.update(updates)
        return data

    def test_validation_rejects_invalid_targets_and_bounds(self):
        for target in ("file:///etc/passwd", "http://user:pass@localhost/", "http://localhost/#fragment"):
            with self.subTest(target=target), self.assertRaises(InvalidTarget):
                parse_monitor(self.config(target=target), self.registry)
        for changes in ({"interval": 5}, {"timeout": 60}, {"threshold": 0}, {"enabled": "yes"}):
            with self.subTest(changes=changes), self.assertRaises(InvalidMonitor):
                parse_monitor(self.config(**changes), self.registry)
        tcp = parse_monitor(self.config(type="tcp", target="[::1]:445"), self.registry)
        self.assertEqual(tcp.target, "[::1]:445")
        with self.assertRaises(InvalidTarget):
            self.service.create(self.config(type="docker", target="not-discovered"))

    def test_threshold_incident_resolution_and_persistence(self):
        monitor_id = self.service.create(self.config())
        monitor = self.repository.get(monitor_id)
        self.service.record_result(monitor, CheckResult(False))
        self.assertEqual(self.repository.get(monitor_id).status, "degraded")
        self.service.record_result(monitor, CheckResult(False))
        self.assertEqual(self.repository.get(monitor_id).status, "down")
        incidents = self.repository.incidents(10)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["severity"], "down")
        self.service.record_result(monitor, CheckResult(True, 12))
        self.assertEqual(self.repository.get(monitor_id).status, "operational")
        self.assertIsNotNone(self.repository.incidents(10)[0]["ended_at"])
        public = self.service.public_status()["monitors"][0]
        self.assertEqual(public["uptime"], 33.33)
        self.assertNotIn("target", public)
        reopened = MonitorRepository(Database(self.path))
        self.assertEqual(reopened.get(monitor_id).status, "operational")
        self.assertEqual(len(reopened.history(monitor_id)), 3)

    def test_docker_mapping_unknown_and_selection(self):
        monitor_id = self.service.create(self.config(name="Background Worker", type="docker",
                                                      target="background-worker", threshold=1))
        monitor = self.repository.get(monitor_id)
        for state, health, expected in (
            ("running", "healthy", "operational"), ("running", "none", "operational"),
            ("running", "starting", "degraded"), ("running", "unhealthy", "down"),
            ("restarting", "none", "degraded"), ("paused", "none", "down"),
            ("exited", "none", "down"), ("dead", "none", "down"),
        ):
            with self.subTest(state=state, health=health):
                result = self.docker_checker.state_result(DockerContainer("background-worker", state, health, None))
                self.assertEqual(result.status, expected)
        self.service.record_result(monitor, self.docker_checker.check_many([monitor])[monitor_id])
        self.assertEqual(self.repository.get(monitor_id).status, "operational")
        self.docker_client.available = False
        self.service.record_result(monitor, self.docker_checker.check_many([monitor])[monitor_id])
        public = self.service.public_status()["monitors"][0]
        self.assertEqual(public["status"], "unknown")
        self.assertEqual(public["uptime"], 100.0)
        self.assertEqual(public["container_name"], "background-worker")
        self.assertNotIn("target", public)
        self.service.update(monitor_id, self.config(name="Renamed", type="docker", target="background-worker"))
        self.assertEqual(self.repository.get(monitor_id).name, "Renamed")

    def test_overall_status_ignores_unknown_monitors_when_known_status_exists(self):
        self.assertEqual(self.service.public_status()['overall'], 'unknown')
        known_id = self.service.create(self.config(name='Measured service'))
        self.service.create(self.config(name='Unmeasured service'))
        self.assertEqual(self.service.public_status()['overall'], 'unknown')
        for status in ('operational', 'degraded', 'down', 'unknown'):
            with self.subTest(status=status):
                success = None if status == 'unknown' else status == 'operational'
                self.service.record_result(self.repository.get(known_id), CheckResult(success, status=status))
                result = self.service.public_status()
                self.assertEqual(result['overall'], status)
                self.assertEqual({m['status'] for m in result['monitors']}, {status, 'unknown'})

    def test_due_cycle_checks_docker_and_handles_proxy_loss(self):
        config = self.config(name="Background Worker", type="docker", target="background-worker", threshold=1)
        monitor_id = self.service.create(config)
        self.assertEqual(self.service.check_due(), 1)
        self.assertEqual(self.repository.get(monitor_id).status, "operational")
        self.docker_client.available = False
        self.service.update(monitor_id, config)  # schedule an immediate recheck
        self.assertEqual(self.service.check_due(), 1)
        self.assertEqual(self.repository.get(monitor_id).status, "unknown")
        self.assertEqual(self.service.public_status()["monitors"][0]["uptime"], 100.0)

    def test_http_and_tcp_checks(self):
        class HealthHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200 if self.path == "/ok" else 503)
                self.end_headers()
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            good_id = self.service.create(self.config(target=f"http://127.0.0.1:{port}/ok"))
            bad_id = self.service.create(self.config(name="Other", target=f"http://127.0.0.1:{port}/bad"))
            self.assertTrue(HttpChecker().check(self.repository.get(good_id)).success)
            self.assertFalse(HttpChecker().check(self.repository.get(bad_id)).success)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            tcp_id = self.service.create(self.config(type="tcp", target=f"127.0.0.1:{port}"))
            result = TcpChecker().check(self.repository.get(tcp_id))
            self.assertTrue(result.success)
            self.assertIsInstance(result.response_ms, int)
        self.assertFalse(TcpChecker().check(self.repository.get(tcp_id)).success)

    def test_authentication_and_csrf(self):
        settings = replace(Settings.from_env({"ADMIN_PASSWORD": "unit-test-only-password-123456"}), db_path=self.path)
        auth = AuthService(settings, self.sessions)
        with patch("status_server.auth.time.sleep"):
            with self.assertRaises(InvalidCredentials):
                auth.login({"username": "admin", "password": "wrong"})
        token, csrf = auth.login({"username": "admin", "password": settings.admin_password})
        cookie = f"status_session={token}"
        self.assertIsNotNone(auth.authenticate(cookie))
        self.assertIsNone(auth.authenticate(cookie, "wrong"))
        session = auth.authenticate(cookie, csrf)
        self.assertIsNotNone(session)
        auth.logout(session)
        self.assertIsNone(auth.authenticate(cookie))

    def test_configuration_validation(self):
        base = {"ADMIN_PASSWORD": "unit-test-only-password-123456"}
        for changes in ({"ADMIN_PASSWORD": "short"}, {"PORT": "nope"}, {"PORT": "65536"},
                        {"COOKIE_SECURE": "maybe"}, {"DOCKER_PROXY_URL": "http://user:pw@host:1/containers"}):
            with self.subTest(changes=changes), self.assertRaises(ConfigurationError):
                Settings.from_env({**base, **changes})

    def test_legacy_schema_migrates_without_losing_history(self):
        legacy = str(Path(self.tmp.name) / "legacy.db")
        with sqlite3.connect(legacy) as db:
            db.execute("""CREATE TABLE monitors (id INTEGER PRIMARY KEY,name TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',
                       type TEXT NOT NULL,target TEXT NOT NULL,interval INTEGER NOT NULL,timeout INTEGER NOT NULL,
                       threshold INTEGER NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,status TEXT NOT NULL DEFAULT 'unknown',
                       response_ms INTEGER,last_checked TEXT,last_success TEXT,state_since TEXT,
                       failures INTEGER NOT NULL DEFAULT 0,next_check REAL NOT NULL DEFAULT 0)""")
            db.execute("INSERT INTO monitors(name,type,target,interval,timeout,threshold) VALUES('Legacy','tcp','localhost:9',60,5,2)")
            db.execute("""CREATE TABLE checks (id INTEGER PRIMARY KEY,monitor_id INTEGER NOT NULL,
                       checked_at TEXT NOT NULL,success INTEGER NOT NULL,response_ms INTEGER,status TEXT NOT NULL)""")
            db.execute("INSERT INTO checks(monitor_id,checked_at,success,status) VALUES(1,'2026-10-07T12:00:00+00:00',1,'operational')")
        Database(legacy).initialize()
        repo = MonitorRepository(Database(legacy))
        self.assertEqual(repo.get(1).name, "Legacy")
        self.assertEqual(len(repo.history(1)), 1)
        self.assertIsNone(repo.get(1).docker_state)

    def test_container_name_migration_keeps_history(self):
        old = MonitorConfig("Status", "", "docker", "status-status-1", 60, 5, 1, True)
        monitor_id = self.repository.create(old)
        monitor = self.repository.get(monitor_id)
        self.service.record_result(monitor, CheckResult(True, status="operational",
                                   docker=DockerContainer("status-status-1", "running", "none", None)))
        self.database.initialize()
        migrated = self.repository.get(monitor_id)
        self.assertEqual(migrated.target, "status-server")
        self.assertEqual(migrated.status, "unknown")
        self.assertEqual(len(self.repository.history(monitor_id)), 1)
        self.database.initialize()
        self.assertEqual(self.repository.get(monitor_id).target, "status-server")


if __name__ == "__main__":
    unittest.main()
