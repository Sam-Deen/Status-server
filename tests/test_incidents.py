import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from health_contract import sanitize_health
from status_server.checks import CheckerRegistry, DockerChecker, HttpChecker, TcpChecker
from status_server.models import CheckResult, DockerContainer, MonitorConfig
from status_server.repository import Database, MonitorRepository
from status_server.service import MonitorService


class IncidentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / 'status.db'))
        self.db.initialize()
        self.repo = MonitorRepository(self.db)
        docker = DockerChecker(None)
        self.service = MonitorService(self.repo, CheckerRegistry([HttpChecker(), TcpChecker(), docker]), docker)
        self.ident = self.repo.create(MonitorConfig('Example', '', 'docker', 'example', 60, 5, 2, True))
        self.start = datetime.now(timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, seconds, status='degraded', core=True, components=None):
        at = self.start + timedelta(seconds=seconds)
        components = components or {
            'application': {'status': 'operational', 'critical': True, 'latencyMs': 12},
            'gameApi': {'status': 'down', 'critical': False, 'reason': 'Request timed out', 'latencyMs': 3000}}
        detail = sanitize_health({'contract': 'application-health/v1', 'timestamp': at.isoformat(),
                                  'validForMs': 90000, 'status': status, 'coreAvailable': core,
                                  'components': components})
        self.assertIsNotNone(detail)
        result = CheckResult(status == 'operational', components['application'].get('latencyMs'), status,
                             DockerContainer('example', 'running', 'healthy', None, detail), core,
                             status == 'operational', at.isoformat(), (at + timedelta(seconds=90)).isoformat())
        with patch('status_server.service.utc_now', return_value=at.isoformat()):
            self.service.record_result(self.repo.get(self.ident), result)

    def incident(self):
        return self.repo.incidents(20, public=True)[0]

    def confirm(self, seconds, *args, **kwargs):
        for offset in (0, 1, 4):
            self.record(seconds + offset, *args, **kwargs)

    def test_optional_component_outage_keeps_core_available_and_excludes_recovery_latency(self):
        self.confirm(0)
        self.record(60, components={
            'application': {'status': 'operational', 'critical': True, 'latencyMs': 18},
            'gameApi': {'status': 'degraded', 'latencyMs': 2500, 'reason': 'Response latency elevated'}})
        self.record(120, 'operational', components={
            'application': {'status': 'operational', 'critical': True, 'latencyMs': 1},
            'gameApi': {'status': 'operational', 'latencyMs': 1}})
        incident = self.service.public_status()['incidents'][0]
        self.assertEqual(incident['ended_reason'], 'recovered')
        detail = incident['details']
        self.assertEqual(detail['check_count'], 4)
        self.assertEqual(detail['core_states'], ['available'])
        self.assertEqual(detail['response_ms'], {'min': 12, 'max': 18})
        component = detail['components']['gameApi']
        self.assertEqual(component['roles'], ['noncritical'])
        self.assertEqual(component['statuses'], ['down', 'degraded'])
        self.assertEqual(component['latency_ms'], {'min': 2500, 'max': 3000})
        self.assertEqual(component['reasons'], ['Request timed out', 'Response latency elevated'])

    def test_escalation_retains_optional_failure_and_adds_critical_database_outage(self):
        self.confirm(0)
        self.record(60, 'down', False, {
            'application': {'status': 'operational', 'critical': True, 'latencyMs': 15},
            'database': {'status': 'down', 'critical': True, 'reason': 'Database query failed'}})
        incident = self.incident()
        self.assertEqual(incident['severity'], 'down')
        self.assertEqual(incident['details']['core_states'], ['available', 'unavailable'])
        self.assertEqual(incident['details']['components']['database']['roles'], ['critical'])
        self.assertIsNone(incident['details']['components']['database']['latency_ms'])
        self.assertIn('gameApi', incident['details']['components'])

    def test_evidence_survives_database_restart_and_raw_check_removal(self):
        self.confirm(0)
        before = self.incident()
        Database(self.db.path).initialize()
        with self.db.connection() as db:
            db.execute('DELETE FROM checks')
        reopened = MonitorRepository(Database(self.db.path))
        self.assertEqual(reopened.incidents(20, public=True)[0], before)

    def test_legacy_incident_reconstructed_from_its_checks_only_and_cached(self):
        self.confirm(0)
        self.record(60, 'operational', components={
            'application': {'status': 'operational', 'critical': True, 'latencyMs': 1}})
        first = self.incident()
        self.record(120, 'down', False, {
            'application': {'status': 'down', 'critical': True, 'latencyMs': 9999, 'reason': 'Event loop stalled'}})
        with self.db.connection() as db:
            db.execute('UPDATE incidents SET details=NULL WHERE id=?', (first['id'],))
        restored = next(item for item in self.repo.incidents(20) if item['id'] == first['id'])
        self.assertEqual(restored['details'], first['details'])
        self.assertNotIn('application', restored['details']['components'])
        with self.db.connection() as db:
            db.execute('DELETE FROM checks')
        self.assertEqual(next(item for item in self.repo.incidents(20) if item['id'] == first['id']), restored)

    def test_legacy_open_incident_backfills_before_appending_without_double_counting(self):
        self.confirm(0)
        with self.db.connection() as db:
            db.execute('UPDATE incidents SET details=NULL')
        self.record(60)
        self.assertEqual(self.incident()['details']['check_count'], 4)

    def test_missing_historical_checks_do_not_borrow_current_components(self):
        self.confirm(0)
        with self.db.connection() as db:
            db.execute('UPDATE incidents SET details=NULL')
            db.execute('DELETE FROM checks')
        detail = self.incident()['details']
        self.assertEqual(detail['check_count'], 0)
        self.assertEqual(detail['components'], {})
        self.assertIsNone(detail['response_ms'])

    def test_stopped_container_does_not_reuse_previous_component_snapshot(self):
        self.confirm(0)
        self.record(60, 'operational', components={
            'application': {'status': 'operational', 'critical': True, 'latencyMs': 12}})
        for seconds in (120, 121, 124):
            at = (self.start + timedelta(seconds=seconds)).isoformat()
            with patch('status_server.service.utc_now', return_value=at):
                self.service.record_result(self.repo.get(self.ident), CheckResult(
                    False, status='down', docker=DockerContainer('example', 'exited', 'none', None),
                    core_available=False, full_available=False))
        detail = self.incident()['details']
        self.assertEqual(detail['sources'], ['docker'])
        self.assertEqual(detail['container_states'], ['exited'])
        self.assertEqual(detail['core_states'], [])
        self.assertEqual(detail['components'], {})
        # Also exercise reconstruction from checks which retained the cached JSON.
        with self.db.connection() as db:
            db.execute('UPDATE incidents SET details=NULL WHERE ended_at IS NULL')
        self.assertEqual(self.incident()['details'], detail)

    def test_passive_failure_is_identified_as_an_observation_not_a_critical_dependency(self):
        self.confirm(0, components={
            'application': {'status': 'degraded', 'critical': True, 'latencyMs': 3000,
                            'reason': 'Response latency elevated'},
            'feature': {'status': 'down', 'measurementType': 'passive', 'reason': 'Operation failed'}})
        detail = self.incident()['details']
        self.assertEqual(detail['components']['feature']['roles'], ['passive'])
        self.assertEqual(detail['components']['application']['roles'], ['critical'])
        self.assertEqual(detail['core_states'], ['available'])

    def test_endpoint_latency_and_failure_reasons_are_retained_without_component_claims(self):
        ident = self.repo.create(MonitorConfig('Web', '', 'http', 'http://localhost', 60, 5, 2, True))
        for seconds, result in ((0, CheckResult(True, 2500)),
                                (60, CheckResult(False, reason='Request timed out')),
                                (120, CheckResult(False, 20, reason='HTTP 503')),
                                (180, CheckResult(True, 1))):
            with patch('status_server.service.utc_now', return_value=(self.start + timedelta(seconds=seconds)).isoformat()):
                self.service.record_result(self.repo.get(ident), result)
        detail = self.incident()['details']
        self.assertEqual(detail['sources'], ['http'])
        self.assertEqual(detail['core_states'], [])
        self.assertEqual(detail['components'], {})
        self.assertEqual(detail['response_ms'], {'min': 20, 'max': 2500})
        self.assertEqual(detail['reasons'], ['Response latency elevated', 'Request timed out', 'HTTP 503'])
        self.assertEqual(self.repo.history(ident)[1]['reason'], 'HTTP 503')

    def test_legacy_checks_without_validity_do_not_establish_component_health(self):
        self.confirm(0)
        with self.db.connection() as db:
            db.execute('UPDATE incidents SET details=NULL')
            db.execute('UPDATE checks SET valid_until=NULL,monitor_type=NULL')
        detail = self.incident()['details']
        self.assertEqual(detail['sources'], ['unknown'])
        self.assertEqual(detail['components'], {})

    def test_monitoring_gap_splits_evidence_between_distinct_incidents(self):
        self.confirm(0)
        first = self.incident()
        self.confirm(600, 'down', False, {
            'application': {'status': 'down', 'critical': True, 'reason': 'Event loop stalled'}})
        incidents = self.repo.incidents(20)
        self.assertEqual(len(incidents), 2)
        ended = next(item for item in incidents if item['id'] == first['id'])
        self.assertEqual(ended['ended_reason'], 'monitoring gap')
        self.assertEqual(ended['details'], first['details'])
        self.assertNotIn('gameApi', self.incident()['details']['components'])

    def test_expired_report_is_not_used_as_historical_component_evidence(self):
        self.confirm(0)
        with self.db.connection() as db:
            db.execute('UPDATE incidents SET details=NULL')
            db.execute('UPDATE checks SET valid_until=?', ((self.start - timedelta(seconds=1)).isoformat(),))
        detail = self.incident()['details']
        self.assertEqual(detail['sources'], ['docker'])
        self.assertEqual(detail['components'], {})

    def test_transport_timeouts_retain_reason_without_inventing_measured_latency(self):
        for kind, target, checker, method in (
                ('http', 'http://localhost', HttpChecker(), 'status_server.checks.http.client.HTTPConnection'),
                ('tcp', 'localhost:80', TcpChecker(), 'status_server.checks.socket.create_connection')):
            ident = self.repo.create(MonitorConfig('Timeout', '', kind, target, 60, 5, 2, True))
            with patch(method) as transport:
                if kind == 'http':
                    transport.return_value.getresponse.side_effect = TimeoutError
                else:
                    transport.side_effect = TimeoutError
                result = checker.check(self.repo.get(ident))
            self.assertFalse(result.success)
            self.assertEqual(result.reason, 'Request timed out')
            self.assertIsNone(result.response_ms)

    def test_reasons_are_filtered_and_disabled_monitors_stay_private(self):
        self.confirm(0, components={
            'application': {'status': 'degraded', 'critical': True, 'latencyMs': 3000,
                            'reason': 'token=secret'}})
        self.assertNotIn('secret', json.dumps(self.incident()))
        with self.db.connection() as db:
            db.execute('UPDATE monitors SET enabled=0')
        self.assertEqual(self.repo.incidents(20, public=True), [])


if __name__ == '__main__':
    unittest.main()
