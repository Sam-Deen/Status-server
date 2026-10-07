"""Validation of administrator supplied monitor configuration."""
from __future__ import annotations

from .checks import CheckerRegistry
from .models import MonitorConfig


class InvalidMonitor(ValueError):
    pass


def parse_monitor(data: object, registry: CheckerRegistry) -> MonitorConfig:
    if not isinstance(data, dict):
        raise InvalidMonitor("Invalid monitor")
    name = str(data.get("name", "")).strip()
    description = str(data.get("description", "")).strip()
    kind = str(data.get("type", "")).lower()
    target = str(data.get("target", "")).strip()
    if not 1 <= len(name) <= 80 or len(description) > 240:
        raise InvalidMonitor("Name must be 1–80 characters; description at most 240")
    checker = registry.for_type(kind)
    if checker is None:
        raise InvalidMonitor("Type must be HTTP, HTTPS, TCP, or Docker Container")
    if not target or len(target) > 2048 or any(ord(char) < 32 or ord(char) == 127 for char in target):
        raise InvalidMonitor("Invalid target")
    checker.validate_target(kind, target)
    try:
        interval = int(data.get("interval", 60))
        timeout = int(data.get("timeout", 5))
        threshold = int(data.get("threshold", 3))
    except (TypeError, ValueError):
        raise InvalidMonitor("Interval, timeout, and threshold must be integers") from None
    if not 10 <= interval <= 3600 or not 1 <= timeout <= 30 or timeout >= interval or not 1 <= threshold <= 10:
        raise InvalidMonitor("Use interval 10–3600s, timeout 1–30s below interval, threshold 1–10")
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise InvalidMonitor("Enabled must be true or false")
    return MonitorConfig(name, description, kind, target, interval, timeout, threshold, enabled)
