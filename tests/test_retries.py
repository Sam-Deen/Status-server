import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from health_contract import sanitize_health
from status_server.checks import Checker, CheckerRegistry, DockerChecker
from status_server.models import CheckResult, DockerContainer, MonitorConfig
from status_server.repository import Database, MonitorRepository
from status_server.scheduler import MonitoringWorker
from status_server.service import MonitorService


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / 'status.db'))
        self.db.initialize()
        self.repo = MonitorRepository(self.db)
        self.docker = DockerChecker(Mock())
        self.service = MonitorService(self.repo, CheckerRegistry([self.docker]), self.docker)
        self.start = datetime.now(timezone.utc)
        self.ident = self.create()

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, kind='http', interval=60, threshold=1):
        return self.repo.create(MonitorConfig('Example', '', kind, 'example', interval, 5, threshold, True))

    @contextmanager
    def at(self, seconds):
        at = self.start + timedelta(seconds=seconds)
        with patch('status_server.service.time.time', return_value=at.timestamp()), \
                patch('status_server.service.utc_now', return_value=at.isoformat()), \
                patch('status_server.service.datetime', wraps=datetime) as clock:
            clock.now.return_value = at
            yield at

    def record(self, seconds, result, ident=None):
        with self.at(seconds):
            self.service.record_result(self.repo.get(ident or self.ident), result)
        return self.repo.get(ident or self.ident)

    def test_exact_backoff_and_confirmation_through_due_checks(self):
        class Probe(Checker):
            kinds = ('http',)
            result = CheckResult(True, 10)
            calls = 0

            def check(self, monitor):
                self.calls += 1
                return self.result

        probe = Probe()
        self.service.registry = CheckerRegistry([probe])
        with self.at(0):
            self.assertEqual(self.service.check_due(), 1)
        probe.result = CheckResult(False, reason='Request timed out')
        seconds = 60
        for index, delay in enumerate((1, 3, 5, 15, 30, 60, 60, 60), 1):
            with self.at(seconds - 0.01):
                self.assertEqual(self.service.check_due(), 0)
            with self.at(seconds):
                self.assertEqual(self.service.check_due(), 1)
                public = self.service.public_status()
            monitor = self.repo.get(self.ident)
            self.assertEqual(monitor.next_check, self.start.timestamp() + seconds + delay)
            self.assertEqual(monitor.failures, index)
            self.assertEqual(monitor.status, 'operational' if index < 3 else 'down')
            self.assertEqual(public['overall'], monitor.status)
            self.assertEqual(len(public['incidents']), 0 if index < 3 else 1)
            if index >= 3:
                self.assertEqual(public['incidents'][0]['details']['check_count'], index)
                self.assertEqual(public['incidents'][0]['started_at'], (self.start + timedelta(seconds=60)).isoformat())
            seconds += delay
        self.assertEqual(probe.calls, 9)
        probe.result = CheckResult(True, 10)
        with self.at(seconds):
            self.assertEqual(self.service.check_due(), 1)
        monitor = self.repo.get(self.ident)
        self.assertEqual((monitor.status, monitor.failures, monitor.retry_attempts), ('operational', 0, 0))
        self.assertEqual(monitor.next_check, self.start.timestamp() + seconds + 60)
        self.assertEqual(self.repo.incidents(10)[0]['ended_reason'], 'recovered')

    def test_one_or_two_failures_leave_no_incident_or_outage_history(self):
        for failures in (1, 2):
            ident = self.create()
            self.record(0, CheckResult(True), ident)
            for seconds in range(1, failures + 1):
                self.record(seconds, CheckResult(False), ident)
            monitor = self.record(4, CheckResult(True), ident)
            self.assertEqual((monitor.failures, monitor.retry_attempts), (0, 0))
            history = self.repo.availability(ident, self.start, self.start + timedelta(seconds=5))['history']
            self.assertEqual(history[0]['status'], 'operational')
        self.assertEqual(self.repo.incidents(10), [])

    def test_all_monitor_types_and_slow_responses_require_three_failures(self):
        for kind, result, expected in (
                ('http', CheckResult(False), 'down'), ('https', CheckResult(False), 'down'),
                ('tcp', CheckResult(False), 'down'), ('http', CheckResult(True, 3000), 'degraded'),
                ('docker', CheckResult(False, status='down', docker=DockerContainer('example', 'exited', 'none', None)), 'down'),
                ('docker', CheckResult(False, status='degraded', docker=DockerContainer('example', 'running', 'starting', None)), 'degraded')):
            with self.subTest(kind=kind, expected=expected):
                ident = self.create(kind)
                for index, seconds in enumerate((0, 1, 4), 1):
                    monitor = self.record(seconds, result, ident)
                    self.assertEqual(monitor.failures, index)
                    self.assertEqual(monitor.status, 'unknown' if index < 3 else expected)
                incident = next(item for item in self.repo.incidents(20) if item['monitor_id'] == ident)
                self.assertEqual(incident['details']['check_count'], 3)

    def test_detailed_health_cannot_bypass_confirmation_in_public_status(self):
        ident = self.create('docker')
        for seconds, status in ((0, 'operational'), (1, 'down'), (2, 'down'), (5, 'down')):
            with self.at(seconds) as at:
                detail = sanitize_health({'contract': 'application-health/v1', 'timestamp': at.isoformat(),
                                          'validForMs': 90000, 'status': status, 'coreAvailable': status == 'operational',
                                          'components': {'application': {'status': status, 'critical': True}}})
                result = self.docker.state_result(DockerContainer('example', 'running', 'healthy', None, detail))
                self.service.record_result(self.repo.get(ident), result)
                item = next(item for item in self.service.public_status()['monitors'] if item['id'] == ident)
                self.assertEqual(item['status'], 'down' if seconds == 5 else 'operational')

    def test_unknown_breaks_streak_but_uses_backoff(self):
        for seconds, result in ((0, CheckResult(False)), (1, CheckResult(False)),
                                (4, CheckResult(None, status='unknown')), (9, CheckResult(False)),
                                (24, CheckResult(False))):
            monitor = self.record(seconds, result)
        self.assertEqual((monitor.failures, monitor.retry_attempts), (2, 5))
        self.assertEqual(self.repo.incidents(10), [])
        self.assertEqual(self.record(54, CheckResult(False)).status, 'down')
        self.assertEqual(self.repo.incidents(10)[0]['details']['check_count'], 3)

    def test_restart_preserves_backoff_and_pending_evidence(self):
        self.record(0, CheckResult(False))
        before = self.record(1, CheckResult(False))
        self.db.initialize()
        self.repo = MonitorRepository(Database(self.db.path))
        self.service = MonitorService(self.repo, CheckerRegistry([self.docker]), self.docker)
        self.assertEqual(self.repo.get(self.ident), before)
        self.record(4, CheckResult(False))
        self.assertEqual(self.repo.incidents(10)[0]['details']['check_count'], 3)

    def test_monitoring_gap_resets_confirmation(self):
        self.record(0, CheckResult(False))
        self.record(1, CheckResult(False))
        monitor = self.record(600, CheckResult(False))
        self.assertEqual((monitor.failures, monitor.retry_attempts), (1, 1))
        self.assertEqual(self.repo.incidents(10), [])

    def test_long_backoff_stays_fresh_with_short_healthy_interval(self):
        ident = self.create(interval=10)
        for seconds in (0, 1, 4, 9, 24, 54):
            monitor = self.record(seconds, CheckResult(False), ident)
        with self.at(113):
            item = next(item for item in self.service.public_status()['monitors'] if item['id'] == ident)
        self.assertEqual(item['status'], 'down')
        self.assertFalse(item['monitoring_stale'])
        self.assertEqual(self.record(114, CheckResult(False), ident).failures, 7)
        monitor = self.record(115, CheckResult(True), ident)
        self.assertEqual(monitor.next_check, self.start.timestamp() + 125)

    def test_worker_never_sleeps_past_one_second_retry(self):
        service, monitors, sessions = Mock(), Mock(), Mock()
        service.check_due.return_value = 0
        worker = MonitoringWorker(service, monitors, sessions, 35)
        worker.stop_event = Mock()
        worker.stop_event.is_set.side_effect = (False, False, True)
        worker._run()
        self.assertEqual([call.args[0] for call in worker.stop_event.wait.call_args_list], [1, 1])


if __name__ == '__main__':
    unittest.main()
