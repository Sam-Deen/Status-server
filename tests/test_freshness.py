import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from health_contract import current_health, sanitize_health, fresh_health
from status_server.checks import Checker, CheckerRegistry, DockerChecker
from status_server.models import CheckResult, DockerContainer, MonitorConfig
from status_server.repository import Database, MonitorRepository
from status_server.scheduler import MonitoringWorker
from status_server.service import MonitorService


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / 'status.db'))
        self.db.initialize()
        self.repository = MonitorRepository(self.db)
        self.docker = DockerChecker(Mock())
        self.service = MonitorService(self.repository, CheckerRegistry([self.docker]), self.docker)
        self.ident = self.repository.create(MonitorConfig('Idle', '', 'docker', 'idle', 60, 5, 3, True))

    def tearDown(self):
        self.tmp.cleanup()

    def test_source_timestamps_survive_roundtrip_and_cached_polling(self):
        time = datetime.now(timezone.utc)
        stamp = time.isoformat()
        component = {'status': 'operational', 'critical': True, 'measurementType': 'active',
                     'intervalMs': 30000, 'staleAfterMs': 90000, 'lastAttempt': stamp,
                     'lastSuccess': stamp, 'lastFailure': None, 'lastMeasurement': stamp, 'lastStatusChange': stamp}
        raw = {'contract': 'application-health/v1', 'status': 'operational', 'coreAvailable': True,
               'timestamp': stamp, 'validForMs': 90000, 'components': {'application': component}}
        detail = sanitize_health(raw)
        result = self.docker.state_result(DockerContainer('idle', 'running', 'healthy', None, detail))
        for seconds in (1, 30):
            with patch('status_server.service.utc_now', return_value=(time + timedelta(seconds=seconds)).isoformat()):
                self.service.record_result(self.repository.get(self.ident), result, stamp)
        monitor = self.repository.get(self.ident)
        self.assertEqual(monitor.last_success, detail['timestamp'])
        self.assertEqual(monitor.last_measurement, detail['timestamp'])
        self.assertEqual(monitor.last_attempt, stamp)
        self.assertIsNone(monitor.last_failure)
        self.assertEqual(self.repository.history(self.ident)[0]['application_health']['components']['application']['lastSuccess'],
                         detail['components']['application']['lastSuccess'])
        self.assertEqual(self.service.public_status()['updated_at'], monitor.last_checked)

    def test_per_component_staleness_is_not_hidden_by_fresh_report_timestamp(self):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(seconds=200)).isoformat()
        raw = {'contract': 'application-health/v1', 'status': 'operational', 'coreAvailable': True,
               'timestamp': now.isoformat(), 'validForMs': 90000,
               'components': {'application': {'status': 'operational', 'critical': True,
                                              'lastMeasurement': old, 'staleAfterMs': 90000}}}
        detail = sanitize_health(raw)
        view = current_health(detail, now)
        self.assertEqual(view['status'], 'unknown')
        self.assertIsNone(view['coreAvailable'])
        self.assertEqual(view['components']['application']['lastMeasurement'], detail['components']['application']['lastMeasurement'])
        self.assertEqual(detail['status'], 'operational')
        self.assertEqual(self.docker.state_result(DockerContainer('idle', 'running', 'healthy', None, detail)).status, 'unknown')

    def test_passive_unknown_does_not_determine_availability(self):
        now = datetime.now(timezone.utc).isoformat()
        raw = {'contract': 'application-health/v1', 'status': 'operational', 'coreAvailable': True,
               'timestamp': now, 'validForMs': 90000, 'components': {
                   'application': {'status': 'operational', 'critical': True},
                   'feature': {'status': 'unknown', 'measurementType': 'passive', 'lastMeasurement': None, 'staleAfterMs': 90000}}}
        detail = sanitize_health(raw)
        self.assertIsNotNone(detail)
        self.assertEqual(current_health(detail)['status'], 'operational')
        raw['components']['feature']['critical'] = True
        self.assertIsNone(sanitize_health(raw))

    def test_transport_freshness_covers_cached_probe_age_and_both_poll_intervals(self):
        now = datetime.now(timezone.utc)
        # 30s cached application measurement + 30s Docker delay + 60s consumer interval.
        stamp = (now - timedelta(seconds=120)).isoformat()
        raw = {'contract': 'application-health/v1', 'status': 'operational', 'coreAvailable': True,
               'timestamp': stamp, 'validForMs': 180000, 'components': {
                   'application': {'status': 'operational', 'critical': True, 'measurementType': 'active',
                                   'lastMeasurement': stamp, 'staleAfterMs': 180000}}}
        detail = sanitize_health(raw)
        self.assertTrue(fresh_health(detail, now))
        self.assertEqual(current_health(detail, now)['status'], 'operational')
        self.assertFalse(fresh_health(detail, now + timedelta(seconds=61)))

    def test_slow_probe_degrades_and_failures_recover_with_distinct_timestamps(self):
        ident = self.repository.create(MonitorConfig('Web', '', 'http', 'http://localhost', 60, 5, 2, True))
        self.service.record_result(self.repository.get(ident), CheckResult(True, 3000))
        monitor = self.repository.get(ident)
        self.assertEqual(monitor.status, 'degraded')
        self.assertIsNotNone(monitor.last_success)
        self.assertIsNone(monitor.last_failure)
        for _ in range(2):
            self.service.record_result(self.repository.get(ident), CheckResult(False))
        self.assertEqual(self.repository.get(ident).status, 'down')
        failure = self.repository.get(ident).last_failure
        self.service.record_result(self.repository.get(ident), CheckResult(True, 20))
        self.assertEqual(self.repository.get(ident).status, 'operational')
        self.assertEqual(self.repository.get(ident).last_failure, failure)

    def test_polling_continues_for_24_hours_without_dashboard_requests(self):
        self.docker.client.list_containers.return_value = [DockerContainer('idle', 'running', 'healthy', None)]
        start = datetime.now(timezone.utc)
        for seconds in range(0, 86400, 60):
            time = start + timedelta(seconds=seconds)
            with patch('status_server.service.time.time', return_value=time.timestamp()), \
                 patch('status_server.service.utc_now', return_value=time.isoformat()):
                self.assertEqual(self.service.check_due(), 1)
        self.assertEqual(self.docker.client.list_containers.call_count, 1440)
        self.assertEqual(len(self.repository.history(self.ident, limit=2000)), 1440)

    def test_worker_retries_a_failed_cycle_independently_of_web_server(self):
        service, monitors, sessions = Mock(), Mock(), Mock()
        done = threading.Event()
        def cycle():
            if service.check_due.call_count == 1:
                raise RuntimeError('temporary failure')
            done.set()
            return 0
        service.check_due.side_effect = cycle
        worker = MonitoringWorker(service, monitors, sessions, 35)
        waits = Mock()
        waits.is_set.side_effect = lambda: done.is_set()
        worker.stop_event = waits
        worker._run()
        self.assertEqual(service.check_due.call_count, 2)
        sessions.cleanup.assert_called_once()

    def test_one_bad_probe_does_not_prevent_other_monitors_being_measured(self):
        class FailingChecker(Checker):
            def check(self, monitor):
                if monitor.id == 1:
                    raise RuntimeError('broken probe')
                return CheckResult(True)
        one, two = Mock(id=1), Mock(id=2)
        results = FailingChecker().check_many([one, two])
        self.assertEqual(results[1].status, 'unknown')
        self.assertTrue(results[2].success)

    def test_new_freshness_configuration_does_not_rewrite_old_history(self):
        self.service.record_result(self.repository.get(self.ident), CheckResult(True))
        with self.db.connection() as db:
            first = dict(db.execute('SELECT * FROM checks ORDER BY id').fetchone())
        self.service.freshness_factor = 3
        self.service.record_result(self.repository.get(self.ident), CheckResult(True))
        with self.db.connection() as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM checks ORDER BY id')]
        self.assertEqual(rows[0], first)
        self.assertEqual(rows[0]['stale_after_seconds'], 120)
        self.assertEqual(rows[1]['stale_after_seconds'], 180)
