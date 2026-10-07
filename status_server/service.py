"""Monitor management, state transitions, and public status assembly."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from .checks import CheckerRegistry, DockerChecker
from .models import CheckResult, Monitor, Transition, utc_now
from .repository import MonitorRepository
from .validation import parse_monitor

LOG = logging.getLogger(__name__)


class MonitorNotFound(Exception):
    pass


class MonitorService:
    def __init__(self, repository: MonitorRepository, registry: CheckerRegistry, docker_checker: DockerChecker):
        self.repository = repository
        self.registry = registry
        self.docker_checker = docker_checker

    def create(self, data: object) -> int:
        config = parse_monitor(data, self.registry)
        self.registry.for_type(config.type).ensure_selection(config.target)
        return self.repository.create(config)

    def update(self, monitor_id: int, data: object) -> None:
        old = self.repository.get(monitor_id)
        if old is None:
            raise MonitorNotFound
        config = parse_monitor(data, self.registry)
        unchanged = old.type == config.type and old.target == config.target
        self.registry.for_type(config.type).ensure_selection(config.target, unchanged)
        reset = not unchanged or (not old.enabled and config.enabled)
        if not self.repository.update(old, config, reset):
            raise MonitorNotFound

    def delete(self, monitor_id: int) -> bool:
        return self.repository.delete(monitor_id)

    def admin_monitors(self) -> list[dict]:
        return [monitor.admin_dict() for monitor in self.repository.all()]

    def discover_containers(self) -> list[dict]:
        return [container.as_dict() for container in self.docker_checker.client.list_containers()]

    def history(self, monitor_id: int) -> list[dict]:
        return self.repository.history(monitor_id)

    def incidents(self) -> list[dict]:
        return self.repository.incidents(100)

    def record_result(self, monitor: Monitor, result: CheckResult) -> None:
        current = self.repository.get(monitor.id)
        if current is None or not current.enabled or current.type != monitor.type or current.target != monitor.target:
            return
        if result.status is not None:
            status, failures = result.status, 0
        else:
            failures = 0 if result.success else current.failures + 1
            status = "operational" if result.success else (
                "down" if failures >= current.threshold else "degraded")
        transition = Transition(status=status, failures=failures, success=status == "operational",
                                checked_at=utc_now(), response_ms=result.response_ms,
                                state_changed=status != current.status, next_check=time.time() + current.interval,
                                docker=result.docker)
        self.repository.save_check(current, transition)

    def check_due(self) -> int:
        monitors = self.repository.due(time.time())
        handled = set()
        for checker, group in self.registry.groups(monitors).items():
            try:
                results = checker.check_many(group)
            except Exception as exc:
                LOG.error("checker failed for %s: %s", ",".join(checker.kinds), type(exc).__name__)
                results = {monitor.id: CheckResult(None, status="unknown") for monitor in group}
            for monitor in group:
                self.record_result(monitor, results.get(monitor.id, CheckResult(None, status="unknown")))
                handled.add(monitor.id)
        for monitor in monitors:
            if monitor.id not in handled:
                self.record_result(monitor, CheckResult(None, status="unknown"))
        return len(monitors)

    def public_status(self) -> dict:
        today = datetime.now(timezone.utc).date()
        days = [(today - timedelta(days=29-offset)).isoformat() for offset in range(30)]
        output = []
        for monitor in self.repository.all(enabled_only=True):
            daily = self.repository.daily_counts(monitor.id, days[0])
            total = sum(value[1] for value in daily.values())
            good = sum(value[0] for value in daily.values())
            item = {"id": monitor.id, "name": monitor.name, "description": monitor.description,
                    "type": monitor.type, "status": monitor.status, "response_ms": monitor.response_ms,
                    "last_checked": monitor.last_checked, "last_success": monitor.last_success,
                    "state_since": monitor.state_since, "uptime": round(100 * good / total, 2) if total else None,
                    "history": [{"day": day, "uptime": round(100 * daily[day][0] / daily[day][1])
                                 if day in daily else None} for day in days]}
            if monitor.type == "docker":
                item.update({"container_name": monitor.target, "container_state": monitor.docker_state or "unknown",
                             "docker_health": monitor.docker_health or "unknown",
                             "last_state_change": monitor.docker_state_since})
            output.append(item)
        statuses = [item["status"] for item in output]
        overall = ("down" if "down" in statuses else "degraded" if "degraded" in statuses else
                   "operational" if statuses and all(status == "operational" for status in statuses) else "unknown")
        return {"overall": overall, "monitors": output, "incidents": self.repository.incidents(20, public=True),
                "updated_at": utc_now()}
