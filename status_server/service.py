"""Monitor management, state transitions, and public status assembly."""
from __future__ import annotations

import logging
import json
import time
from datetime import datetime, timedelta, timezone

from .checks import CheckerRegistry, DockerChecker
from .models import CheckResult, Monitor, Transition, utc_now
from .repository import MonitorRepository
from .validation import parse_monitor
from health_contract import fresh_health, instant, current_health

LOG = logging.getLogger(__name__)


class MonitorNotFound(Exception):
    pass


class MonitorService:
    def __init__(self, repository: MonitorRepository, registry: CheckerRegistry, docker_checker: DockerChecker,
                 freshness_factor: float = 2, slow_ms: int = 2000):
        self.repository = repository
        self.registry = registry
        self.docker_checker = docker_checker
        self.availability_cache = {}
        self.freshness_factor = freshness_factor
        self.slow_ms = slow_ms

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
        self.availability_cache.pop(monitor_id, None)
        return self.repository.delete(monitor_id)

    def admin_monitors(self) -> list[dict]:
        return [monitor.admin_dict() for monitor in self.repository.all()]

    def discover_containers(self) -> list[dict]:
        return [container.as_dict() for container in self.docker_checker.client.list_containers()]

    def history(self, monitor_id: int) -> list[dict]:
        return self.repository.history(monitor_id)

    def incidents(self) -> list[dict]:
        return self.repository.incidents(100)

    def record_result(self, monitor: Monitor, result: CheckResult, attempted_at: str | None = None) -> None:
        current = self.repository.get(monitor.id)
        if current is None or not current.enabled or current.type != monitor.type or current.target != monitor.target:
            return
        if result.status is not None:
            status, failures = result.status, 0
        else:
            failures = 0 if result.success else current.failures + 1
            status = "operational" if result.success else (
                "down" if failures >= current.threshold else "degraded")
            if result.success and result.response_ms is not None and result.response_ms > self.slow_ms:
                status = 'degraded'
        checked_at = utc_now()
        if result.docker is not None:
            core, full = result.core_available, result.full_available
            if current.application_health and not result.docker.application_health and result.docker.state == 'running':
                status = 'unknown'
                core = full = None
        else:
            core = result.success if status != 'unknown' else None
            full = status == 'operational' if status != 'unknown' else None
        transition = Transition(status=status, failures=failures, success=status == "operational",
                                checked_at=checked_at, response_ms=result.response_ms,
                                state_changed=status != current.status, next_check=time.time() + current.interval,
                                docker=result.docker, core_available=core, full_available=full,
                                observed_at=result.observed_at or checked_at, valid_until=result.valid_until,
                                attempted_at=result.attempted_at or attempted_at or checked_at, failed=result.success is not True,
                                stale_after_seconds=current.interval * self.freshness_factor)
        self.repository.save_check(current, transition)

    def check_due(self) -> int:
        monitors = self.repository.due(time.time())
        handled = set()
        for checker, group in self.registry.groups(monitors).items():
            attempted_at = utc_now()
            try:
                results = checker.check_many(group)
            except Exception as exc:
                LOG.error("checker failed for %s: %s", ",".join(checker.kinds), type(exc).__name__)
                results = {monitor.id: CheckResult(None, status="unknown") for monitor in group}
            for monitor in group:
                try:
                    self.record_result(monitor, results.get(monitor.id, CheckResult(None, status="unknown")), attempted_at)
                except Exception as exc:
                    LOG.error("could not persist check for %s: %s", monitor.id, type(exc).__name__)
                handled.add(monitor.id)
        for monitor in monitors:
            if monitor.id not in handled:
                self.record_result(monitor, CheckResult(None, status="unknown"))
        return len(monitors)

    def public_status(self) -> dict:
        today = datetime.now(timezone.utc).date()
        days = [(today - timedelta(days=29-offset)).isoformat() for offset in range(30)]
        output = []
        observation_expiries = {}
        now = datetime.now(timezone.utc)
        for monitor in self.repository.all(enabled_only=True):
            daily = self.repository.daily_counts(monitor.id, days[0])
            total = sum(value[1] for value in daily.values())
            good = sum(value[0] for value in daily.values())
            item = {"id": monitor.id, "name": monitor.name, "description": monitor.description,
                    "type": monitor.type, "status": monitor.status, "response_ms": monitor.response_ms,
                    "last_checked": monitor.last_checked, "last_success": monitor.last_success,
                    "last_attempt": monitor.last_attempt, "last_failure": monitor.last_failure,
                    "last_measurement": monitor.last_measurement, "last_status_change": monitor.state_since,
                    "state_since": monitor.state_since, "uptime": round(100 * good / total, 2) if total else None,
                    "history": [{"day": day, "uptime": round(100 * daily[day][0] / daily[day][1])
                                 if day in daily else None} for day in days]}
            if monitor.type == "docker":
                item.update({"container_name": monitor.target, "container_state": monitor.docker_state or "unknown",
                             "docker_health": monitor.docker_health or "unknown",
                             "last_state_change": monitor.docker_state_since})
            cache = self.availability_cache.get(monitor.id)
            if cache is None or time.monotonic() - cache[0] >= 30:
                start = now - timedelta(days=30)
                availability = self.repository.availability(monitor.id, start, now)
                self.availability_cache[monitor.id] = (time.monotonic(), availability)
            else:
                availability = cache[1]
            item["measured_availability"] = availability
            detail = json.loads(monitor.application_health) if monitor.application_health else None
            if monitor.last_checked:
                expiry = instant(monitor.last_checked) + timedelta(seconds=self.freshness_factor * monitor.interval)
                if detail and monitor.application_health_valid:
                    expiry = min(expiry, instant(detail['timestamp']) + timedelta(milliseconds=detail['validForMs']))
                observation_expiries[monitor.id] = expiry
            stale = not monitor.last_checked or (now - instant(monitor.last_checked)).total_seconds() > self.freshness_factor * monitor.interval
            if stale:
                item["status"] = 'unknown'
            if detail:
                fresh = monitor.application_health_valid and not stale and fresh_health(detail, now, monitor.docker_state_since)
                if monitor.application_health_valid and not fresh and not stale:
                    item['status'] = 'unknown' if monitor.docker_state == 'running' else item['status']
                if fresh:
                    detail = current_health(detail, now)
                    item['status'] = detail['status']
                item["application_health"] = detail if fresh else {
                    "status": "unknown", "timestamp": detail['timestamp'], "coreAvailable": None,
                    "components": {name: {**component, "status": "unknown", "reason": "Monitoring result stale", "stale": True}
                                   for name, component in detail['components'].items()}}
            item["monitoring_stale"] = stale
            output.append(item)
        statuses = [item["status"] for item in output]
        overall = ("down" if "down" in statuses else "degraded" if "degraded" in statuses else
                   "operational" if "operational" in statuses else "unknown")
        incidents = self.repository.incidents(20, public=True)
        for incident in incidents:
            expiry = observation_expiries.get(incident['monitor_id'])
            if not incident['ended_at'] and expiry and expiry < now:
                incident['ended_at'] = expiry.isoformat()
                incident['ended_reason'] = 'monitoring gap'
        latest = max((item['last_checked'] for item in output if item['last_checked']), default=None)
        return {"overall": overall, "monitors": output, "incidents": incidents,
                "updated_at": latest}
