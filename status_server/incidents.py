"""Compact incident evidence from impaired checks, never from current monitor state."""
from __future__ import annotations

import re
from copy import deepcopy

from health_contract import REASONS, current_health, fresh_health, instant, sanitize_health


def remember(values: list, value) -> None:
    if value not in values:
        values.append(value)


def latency_range(previous: dict | None, value: float | None) -> dict | None:
    if value is None:
        return previous
    return {'min': min(previous['min'], value), 'max': max(previous['max'], value)} if previous else {
        'min': value, 'max': value}


def remember_reason(values: list, reason: str | None) -> None:
    if isinstance(reason, str) and (reason in REASONS or re.fullmatch(r'HTTP [45][0-9]{2}', reason)):
        remember(values, reason)


def merge_details(previous: dict | None, check: dict) -> dict:
    """Retain all observed causes and latency ranges, excluding recovery checks.

    A cached snapshot in a stopped container or a check without source validity
    is not evidence of component health at the time of that check.
    """
    details = deepcopy(previous) if previous else {
        'check_count': 0, 'sources': [], 'core_states': [], 'reasons': [],
        'response_ms': None, 'components': {}, 'container_states': [], 'container_health': []}
    if check['status'] not in {'degraded', 'down'}:
        return details
    details['check_count'] += 1
    details['response_ms'] = latency_range(details['response_ms'], check.get('response_ms'))
    remember_reason(details['reasons'], check.get('reason'))
    detail = sanitize_health(check.get('application_health')) if check.get('valid_until') else None
    if detail:
        at = instant(check['checked_at'])
        if fresh_health(detail, at) and at <= instant(check['valid_until']):
            detail = current_health(detail, at)
        else:
            detail = None
    if detail and detail['status'] == check['status']:
        remember(details['sources'], 'application')
        remember(details['core_states'], 'available' if detail['coreAvailable'] is True else
                 'unavailable' if detail['coreAvailable'] is False else 'unknown')
        for name, component in detail['components'].items():
            if component['status'] == 'operational':
                continue
            item = details['components'].setdefault(name, {
                'statuses': [], 'roles': [], 'reasons': [], 'latency_ms': None,
                'first_observed': detail['timestamp'], 'last_observed': detail['timestamp']})
            remember(item['statuses'], component['status'])
            remember(item['roles'], 'passive' if component.get('measurementType') == 'passive' else
                     'critical' if component['critical'] else 'noncritical')
            remember_reason(item['reasons'], component.get('reason'))
            if component['status'] != 'unknown':
                item['latency_ms'] = latency_range(item['latency_ms'], component.get('latencyMs'))
            item['last_observed'] = detail['timestamp']
    else:
        kind = check.get('monitor_type') or 'unknown'
        remember(details['sources'], 'http' if kind == 'https' else kind)
    for field, source in (('container_states', 'docker_state'), ('container_health', 'docker_health')):
        if check.get(source):
            remember(details[field], check[source])
    return details
