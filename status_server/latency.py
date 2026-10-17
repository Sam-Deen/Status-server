"""Public numeric latency series, without private check details."""
from __future__ import annotations

import json
from datetime import timedelta

from health_contract import bounded_number, current_health, instant


def latency_series(checks: list[dict], monitor_type: str) -> list[dict]:
    samples = []
    names = set()
    previous_expiry = None
    for check in checks:
        checked_at = instant(check['checked_at'])
        stamp = round(checked_at.timestamp() * 1000)
        values = {}
        kind = check['monitor_type'] or monitor_type
        if kind == 'docker':
            # Old health snapshots may be retained on a failed container check.
            # Only graph components from a report valid at the time of this check.
            if (check['application_health'] and check['valid_until']
                    and instant(check['valid_until']) >= checked_at and check['docker_state'] == 'running'):
                health = current_health(json.loads(check['application_health']), checked_at)
                for name, component in health['components'].items():
                    value = component.get('latencyMs')
                    if not component.get('stale') and component['status'] != 'unknown' and bounded_number(value, 86400000):
                        values['component:' + name] = value
        elif bounded_number(check['response_ms'], 86400000):
            values['response'] = check['response_ms']
        if previous_expiry is not None and checked_at > previous_expiry:
            samples.append((round(previous_expiry.timestamp() * 1000), {}))
        samples.append((stamp, values))
        names.update(values)
        previous_expiry = checked_at + timedelta(seconds=check['stale_after_seconds'] or
                                                 2 * (check['interval_seconds'] or 60))
        if check['valid_until'] and instant(check['valid_until']) >= checked_at:
            previous_expiry = min(previous_expiry, instant(check['valid_until']))
    return [{'key': name, 'label': 'Response time' if name == 'response' else name.removeprefix('component:'),
             'points': [[stamp, values.get(name)] for stamp, values in samples]}
            for name in sorted(names)]
