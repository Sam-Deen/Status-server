"""HTTP assertions for isolated Docker fixtures; run inside the status container.

Requires the three offline Monkee healthBot.js fixtures named PREFIX-operational,
PREFIX-degraded and PREFIX-down. This script never calls external dependencies.
"""
import http.cookiejar
import json
import sys
import time
import urllib.request


def main(prefix, extra_names=()):
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(path, body=None, csrf=None):
        headers = {'Content-Type': 'application/json'}
        if csrf:
            headers['X-CSRF-Token'] = csrf
        req = urllib.request.Request('http://127.0.0.1:8080' + path, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        with opener.open(req, timeout=3) as response:
            return json.load(response)

    login = request('/api/admin/login', {'username': 'admin', 'password': 'temporary-validation-password-123456'})
    existing = request('/api/admin/monitors')['monitors']
    for state in ('operational', 'degraded', 'down'):
        name = prefix + '-' + state
        if not any(monitor['name'] == name for monitor in existing):
            request('/api/admin/monitors', {'name': name, 'description': 'Offline health fixture',
                    'type': 'docker', 'target': name, 'interval': 10, 'timeout': 5, 'threshold': 3,
                    'enabled': True}, login['csrf'])
    for name in extra_names:
        if not any(monitor['name'] == name for monitor in existing):
            request('/api/admin/monitors', {'name': name, 'description': 'Application health fixture',
                    'type': 'docker', 'target': name, 'interval': 10, 'timeout': 5, 'threshold': 3,
                    'enabled': True}, login['csrf'])
    deadline = time.monotonic() + 20
    while True:
        data = request('/api/status')
        monitors = {item['name'].removeprefix(prefix + '-'): item for item in data['monitors']}
        if (all(monitors[state]['status'] == state for state in ('operational', 'degraded', 'down'))
                and all(monitors[name.removeprefix(prefix + '-')]['status'] == 'operational' for name in extra_names)):
            break
        if time.monotonic() >= deadline:
            raise AssertionError({key: item['status'] for key, item in monitors.items()})
        time.sleep(0.2)
    assert monitors['operational']['application_health']['coreAvailable'] is True
    assert monitors['degraded']['docker_health'] == 'healthy'
    assert monitors['degraded']['application_health']['components']['gameApi']['status'] == 'down'
    assert monitors['down']['application_health']['coreAvailable'] is False
    assert request('/healthz')['ok'] is True
    for item in monitors.values():
        history = request(f"/api/admin/monitors/{item['id']}/history")['checks']
        assert history and history[0]['application_health']
    initial = {item['id']: item['last_checked'] for item in monitors.values()}
    # No dashboard/status HTTP requests while the real worker keeps polling.
    time.sleep(12)
    for item in request('/api/status')['monitors']:
        assert item['last_checked'] > initial[item['id']]
        if item['name'] in extra_names:
            for component in item['application_health']['components'].values():
                assert component['lastSuccess'] and component['lastMeasurement']
    print(json.dumps({'application_statuses': {state: monitors[state]['status'] for state in monitors},
                      'degraded_docker_health': monitors['degraded']['docker_health'], 'status_server_healthy': True}))


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2:])
