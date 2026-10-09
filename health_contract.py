"""Bounded application-health/v1 parser shared by the filtered proxy and consumer.

Never expose arbitrary Docker output, URLs, SQL, request bodies, or raw exceptions.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone

STATUSES = {"operational", "degraded", "down", "unknown"}
REASONS = {"Not measured", "Monitoring result stale", "Response latency elevated", "Operation failed",
           "Database contention", "Database query failed", "Request timed out", "Rate limited",
           "Connection failed", "Recovering", "Request still pending", "Event loop stalled",
           "Gateway heartbeat not measured", "Discord disconnected", "Discord reconnecting",
           "Invalid API response", "Interaction dispatch failed", "Health endpoint unavailable"}


def instant(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Invalid timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timezone required")
    return result.astimezone(timezone.utc)


def bounded_number(value: object, maximum: float) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= maximum


def sanitize_health(value: object) -> dict | None:
    try:
        if not isinstance(value, dict) or value.get("contract") != "application-health/v1":
            return None
        status, core = value["status"], value["coreAvailable"]
        if status not in STATUSES or (core is not None and not isinstance(core, bool)):
            return None
        if (status == "down" and core is not False) or (status == "operational" and core is not True):
            return None
        stamp = instant(value["timestamp"]).isoformat(timespec="milliseconds")
        ttl = value["validForMs"]
        if not bounded_number(ttl, 300000) or ttl < 1000:
            return None
        raw = value["components"]
        if not isinstance(raw, dict) or not 1 <= len(raw) <= 16:
            return None
        components = {}
        for name, component in raw.items():
            if not isinstance(name, str) or not re.fullmatch(r"[a-z][A-Za-z0-9]{0,31}", name):
                return None
            if not isinstance(component, dict) or component.get("status") not in STATUSES:
                return None
            critical = component.get("critical", False)
            if not isinstance(critical, bool):
                return None
            item = {"status": component["status"], "critical": critical}
            latency = component.get("latencyMs")
            if latency is not None and not bounded_number(latency, 86400000):
                return None
            item["latencyMs"] = latency
            reason = component.get("reason")
            item["reason"] = reason if isinstance(reason, str) and (
                reason in REASONS or re.fullmatch(r"HTTP [45][0-9]{2}", reason)) else None
            for key in ("lastAttempt", "lastSuccess"):
                item[key] = instant(component[key]).isoformat(timespec="milliseconds") if component.get(key) else None
            for key in ("consecutiveFailures", "consecutiveRecoveries"):
                counter = component.get(key, 0)
                if not isinstance(counter, int) or isinstance(counter, bool) or not 0 <= counter <= 1000000:
                    return None
                item[key] = counter
            if 'reconnectAttempts' in component:
                counter = component['reconnectAttempts']
                if not isinstance(counter, int) or isinstance(counter, bool) or not 0 <= counter <= 1000000:
                    return None
                item['reconnectAttempts'] = counter
            components[name] = item
        critical = [item for item in components.values() if item['critical']]
        if core is True and any(item['status'] in {'down', 'unknown'} for item in critical):
            return None
        if core is False and status != 'down':
            return None
        if status == 'operational' and any(item['status'] != 'operational' for item in components.values()):
            return None
        return {"contract": "application-health/v1", "status": status, "coreAvailable": core,
                "timestamp": stamp, "validForMs": ttl, "components": components}
    except (ValueError, TypeError, KeyError, OverflowError):
        return None


def fresh_health(value: dict, now: datetime | None = None, started_at: str | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    try:
        stamp = instant(value["timestamp"])
        age = (now - stamp).total_seconds() * 1000
        return -5000 <= age <= value["validForMs"] and (not started_at or stamp >= instant(started_at))
    except (ValueError, TypeError, KeyError):
        return False
