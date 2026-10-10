"""Small data objects shared by monitoring, persistence, and the API."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class MonitorConfig:
    name: str
    description: str
    type: str
    target: str
    interval: int
    timeout: int
    threshold: int
    enabled: bool

    def db_values(self) -> tuple:
        return (self.name, self.description, self.type, self.target, self.interval,
                self.timeout, self.threshold, int(self.enabled))


@dataclass(frozen=True)
class Monitor:
    id: int
    name: str
    description: str
    type: str
    target: str
    interval: int
    timeout: int
    threshold: int
    enabled: bool
    status: str
    response_ms: int | None
    last_checked: str | None
    last_success: str | None
    state_since: str | None
    failures: int
    next_check: float
    docker_state: str | None
    docker_health: str | None
    docker_state_since: str | None
    application_health: str | None = None
    application_health_valid: bool = False
    last_attempt: str | None = None
    last_failure: str | None = None
    last_measurement: str | None = None

    @classmethod
    def from_record(cls, record: Mapping) -> Monitor:
        return cls(**{key: bool(record[key]) if key in {"enabled", "application_health_valid"} else record[key]
                      for key in cls.__dataclass_fields__})

    def admin_dict(self) -> dict:
        return {key: int(value) if key == "enabled" else value
                for key, value in vars(self).items()}


@dataclass(frozen=True)
class DockerContainer:
    name: str
    state: str
    health: str
    state_since: str | None
    application_health: dict | None = None

    def as_dict(self) -> dict:
        return vars(self).copy()


@dataclass(frozen=True)
class CheckResult:
    success: bool | None
    response_ms: int | None = None
    status: str | None = None
    docker: DockerContainer | None = None
    core_available: bool | None = None
    full_available: bool | None = None
    observed_at: str | None = None
    valid_until: str | None = None
    attempted_at: str | None = None


@dataclass(frozen=True)
class Transition:
    status: str
    failures: int
    success: bool
    checked_at: str
    response_ms: int | None
    state_changed: bool
    next_check: float
    docker: DockerContainer | None
    core_available: bool | None = None
    full_available: bool | None = None
    observed_at: str | None = None
    valid_until: str | None = None
    attempted_at: str | None = None
    failed: bool = False
    stale_after_seconds: float | None = None
