import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from status_server.checks import CheckerRegistry, DockerChecker, HttpChecker
from status_server.models import CheckResult, MonitorConfig
from status_server.repository import Database, MonitorRepository
from status_server.service import MonitorService


class LatencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        database = Database(str(Path(self.tmp.name) / 'status.db'))
        database.initialize()
        self.repository = MonitorRepository(database)
        self.service = MonitorService(self.repository, CheckerRegistry([HttpChecker()]), DockerChecker(None))
        self.end = datetime.now(timezone.utc)
        self.start = self.end - timedelta(hours=24)

    def tearDown(self):
        self.tmp.cleanup()

    def monitor(self, kind='http', enabled=True):
        return self.repository.create(MonitorConfig('Example', '', kind, 'private-target', 60, 5, 3, enabled))

    def check(self, ident, at, value, kind='http', health=None, expiry=None, state=None):
        with self.repository.database.connection() as db:
            db.execute('''INSERT INTO checks(monitor_id,checked_at,success,response_ms,status,monitor_type,
                          interval_seconds,stale_after_seconds,application_health,valid_until,docker_state)
                          VALUES(?,?,1,?,'operational',?,60,120,?,?,?)''',
                       (ident, at.isoformat(), value, kind, json.dumps(health) if health else None,
                        expiry.isoformat() if expiry else None, state))

    def public(self):
        with patch('status_server.service.datetime') as clock:
            clock.now.return_value = self.end
            return self.service.public_latency()

    def test_full_24_hour_window_includes_healthy_slow_failed_and_zero_measurements(self):
        ident = self.monitor()
        self.check(ident, self.start - timedelta(seconds=1), 99999)
        self.check(ident, self.end + timedelta(seconds=1), 99999)
        for minute in range(240):
            self.check(ident, self.start + timedelta(minutes=minute), minute)
        for seconds, result in enumerate([CheckResult(True, 0), CheckResult(True, 4000), CheckResult(False, 503)]):
            at = self.end - timedelta(seconds=3-seconds)
            with patch('status_server.service.utc_now', return_value=at.isoformat()):
                self.service.record_result(self.repository.get(ident), result)
        data = self.public()
        self.assertEqual(data['from'], self.start.isoformat())
        self.assertEqual(data['to'], self.end.isoformat())
        series = data['monitors'][0]['series']
        self.assertEqual(len(series), 1)
        values = [value for _, value in series[0]['points'] if value is not None]
        self.assertEqual(len(values), 243)  # The admin endpoint only returns 200.
        self.assertEqual(values[-3:], [0, 4000, 503])
        self.assertNotIn(99999, values)
        self.assertNotIn('private-target', json.dumps(data))

    def test_missing_measurements_and_monitoring_gaps_break_lines(self):
        ident = self.monitor()
        for minute, value in [(0, 10), (1, None), (2, 20), (10, 30)]:
            self.check(ident, self.start + timedelta(minutes=minute), value)
        points = self.public()['monitors'][0]['series'][0]['points']
        self.assertEqual([value for _, value in points], [10, None, 20, None, 30])
        self.assertEqual(points[3][0], round((self.start + timedelta(minutes=4)).timestamp() * 1000))

    def test_component_series_overlap_without_duplicating_application_latency_or_replaying_stale_data(self):
        ident = self.monitor('docker')
        health = {'timestamp': self.start.isoformat(), 'components': {
            'application': {'status': 'operational', 'latencyMs': 12, 'critical': True},
            'database': {'status': 'degraded', 'latencyMs': 2500, 'critical': False},
            'gateway': {'status': 'operational', 'latencyMs': 40, 'critical': False,
                        'lastMeasurement': self.start.isoformat(), 'staleAfterMs': 1000, 'measurementType': 'passive'},
            'unmeasured': {'status': 'operational', 'latencyMs': None, 'critical': False}}}
        expiry = self.start + timedelta(minutes=2)
        self.check(ident, self.start, 12, 'docker', health, expiry, 'running')
        self.check(ident, self.start + timedelta(minutes=1), 12, 'docker', health, expiry, 'running')
        self.check(ident, self.start + timedelta(minutes=2), None, 'docker', health, None, 'exited')
        self.check(ident, self.start + timedelta(minutes=3), None, 'docker', health, expiry, 'running')
        series = {item['label']: item['points'] for item in self.public()['monitors'][0]['series']}
        self.assertEqual(set(series), {'application', 'database', 'gateway'})
        self.assertEqual([value for _, value in series['application']], [12, 12, None, None])
        self.assertEqual([value for _, value in series['database']], [2500, 2500, None, None])
        self.assertEqual([value for _, value in series['gateway']], [40, None, None, None])

    def test_disabled_services_are_excluded_and_empty_services_have_no_series(self):
        self.check(self.monitor(enabled=False), self.start, 1000)
        ident = self.monitor()
        data = self.public()
        self.assertEqual(data['monitors'], [{'id': ident, 'name': 'Example', 'series': []}])
