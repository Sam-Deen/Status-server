"""Periodic check and retention worker."""
from __future__ import annotations

import logging
import threading
import time

from .repository import MonitorRepository, SessionRepository
from .service import MonitorService

LOG = logging.getLogger(__name__)


class MonitoringWorker:
    def __init__(self, service: MonitorService, monitors: MonitorRepository,
                 sessions: SessionRepository, retention_days: int):
        self.service = service
        self.monitors = monitors
        self.sessions = sessions
        self.retention_days = retention_days
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="monitoring-worker", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=2)

    def _run(self) -> None:
        last_cleanup = 0.0
        while not self.stop_event.is_set():
            count = 0
            try:
                count = self.service.check_due()
                if time.time() - last_cleanup >= 3600:
                    self.monitors.cleanup(self.retention_days)
                    self.sessions.cleanup()
                    last_cleanup = time.time()
            except Exception as exc:
                LOG.error("monitoring cycle failed: %s", type(exc).__name__)
            self.stop_event.wait(1 if count else 3)
