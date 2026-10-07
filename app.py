"""Status Server entry point."""
from __future__ import annotations

import logging
import signal
import threading
from pathlib import Path

from status_server.auth import AuthService
from status_server.checks import CheckerRegistry, DockerChecker, DockerProxyClient, HttpChecker, TcpChecker
from status_server.config import ConfigurationError, Settings
from status_server.repository import Database, MonitorRepository, SessionRepository
from status_server.scheduler import MonitoringWorker
from status_server.service import MonitorService
from status_server.web import StatusHTTPServer


def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigurationError as exc:
        raise SystemExit(str(exc)) from None
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    database = Database(settings.db_path)
    database.initialize()
    monitors = MonitorRepository(database)
    sessions = SessionRepository(database)
    docker_checker = DockerChecker(DockerProxyClient(settings.docker_proxy_url))
    registry = CheckerRegistry([HttpChecker(), TcpChecker(), docker_checker])
    service = MonitorService(monitors, registry, docker_checker)
    auth = AuthService(settings, sessions)
    server = StatusHTTPServer(settings, service, auth, database, Path(__file__).resolve().parent / "static")
    worker = MonitoringWorker(service, monitors, sessions, settings.retention_days)

    def stop_server(*_args) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    worker.start()
    logging.info("Status Server listening on %s:%s", settings.host, settings.port)
    try:
        server.serve_forever()
    finally:
        worker.stop()
        server.server_close()


if __name__ == "__main__":
    main()
