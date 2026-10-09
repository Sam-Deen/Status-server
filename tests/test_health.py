import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import proxy
from health_contract import sanitize_health, fresh_health
from status_server.availability import measured_availability
from status_server.checks import CheckerRegistry, DockerChecker, DockerProxyClient, DockerUnavailable, HttpChecker, TcpChecker
from status_server.models import DockerContainer, MonitorConfig
from status_server.repository import Database, MonitorRepository
from status_server.service import MonitorService


def snapshot(status='operational', core=True, timestamp=None):
    stamp = timestamp or datetime.now(timezone.utc).isoformat()
    return {'contract': 'application-health/v1', 'status': status, 'coreAvailable': core,
            'timestamp': stamp, 'validForMs': 90000,
            'components': {'application': {'status': 'operational', 'critical': True, 'latencyMs': 12},
                           'gameApi': {'status': 'down' if status == 'degraded' else 'operational',
                                       'reason': 'Request timed out' if status == 'degraded' else None}}}


class ContractTests(unittest.TestCase):
    def test_filtered_inspect_accepts_contract_and_never_exposes_arbitrary_output(self):
        raw = snapshot('degraded')
        raw['secret'] = 'credential'
        raw['components']['application']['reason'] = 'token=secret'
        inspect = {'Name': '/monkee', 'State': {'Status': 'running', 'Health': {
            'Status': 'healthy', 'Log': [{'Output': json.dumps(raw)}]}}}
        result = proxy.sanitized_container(inspect)
        self.assertEqual(result['application_health']['components']['gameApi']['status'], 'down')
        self.assertNotIn('credential', str(result))
        self.assertNotIn('token=secret', str(result))
        for output in ('secret', '{', 'x' * 4097, json.dumps({'status': 'operational'})):
            inspect['State']['Health']['Log'] = [{'Output': output}]
            self.assertNotIn('application_health', proxy.sanitized_container(inspect))

    def test_invalid_versions_types_limits_and_timestamps_are_rejected(self):
        for changes in ({'contract': 'other'}, {'status': 'healthy'}, {'coreAvailable': 'true'},
                        {'timestamp': '2026-10-10'}, {'validForMs': float('nan')},
                        {'components': {'a': {'status': 'operational', 'latencyMs': -1}}},
                        {'components': {'a': {'status': 'operational', 'consecutiveFailures': True}}},
                        {'components': {f'c{i}': {'status': 'operational'} for i in range(17)}}):
            with self.subTest(changes=changes):
                self.assertIsNone(sanitize_health({**snapshot(), **changes}))

    def test_freshness_checks_future_timestamps_and_container_recreation(self):
        now = datetime.now(timezone.utc)
        self.assertTrue(fresh_health(snapshot(timestamp=now.isoformat()), now))
        self.assertFalse(fresh_health(snapshot(timestamp=(now - timedelta(seconds=91)).isoformat()), now))
        self.assertFalse(fresh_health(snapshot(timestamp=(now + timedelta(seconds=6)).isoformat()), now))
        self.assertFalse(fresh_health(snapshot(timestamp=(now - timedelta(seconds=2)).isoformat()), now,
                                      now.isoformat()))


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)

    def row(self, seconds, core, full, **changes):
        row = {'checked_at': (self.start + timedelta(seconds=seconds)).isoformat(),
               'core_available': core, 'full_available': full, 'interval_seconds': 60, 'valid_until': None}
        row.update(changes)
        return row

    def test_degraded_core_availability_differs_from_full_and_unknown_is_excluded(self):
        rows = [self.row(0, True, True), self.row(60, True, False), self.row(120, False, False),
                self.row(180, None, None)]
        result = measured_availability(rows, self.start, self.start + timedelta(seconds=240))
        self.assertEqual(result['core']['availability'], 66.67)
        self.assertEqual(result['full']['availability'], 33.33)
        self.assertEqual(result['core']['coverage'], 75)
        self.assertEqual(result['core']['unknown_seconds'], 60)

    def test_monitoring_gap_cannot_extend_a_success_indefinitely(self):
        result = measured_availability([self.row(0, True, True)], self.start, self.start + timedelta(seconds=600))
        self.assertEqual(result['core']['measured_seconds'], 120)
        self.assertEqual(result['core']['coverage'], 20)
        self.assertEqual(result['core']['unknown_seconds'], 480)

    def test_duplicate_application_snapshots_do_not_extend_source_expiry(self):
        expiry = (self.start + timedelta(seconds=90)).isoformat()
        rows = [self.row(seconds, True, True, valid_until=expiry) for seconds in (0, 60, 120)]
        result = measured_availability(rows, self.start, self.start + timedelta(seconds=180))
        self.assertEqual(result['core']['measured_seconds'], 90)
        self.assertEqual(result['full']['coverage'], 50)

    def test_disable_boundary_stops_coverage_and_legacy_rows_are_preserved_without_inference(self):
        rows = [self.row(0, True, True), self.row(10, None, None, interval_seconds=0),
                self.row(20, None, None, interval_seconds=None)]
        result = measured_availability(rows, self.start, self.start + timedelta(seconds=60))
        self.assertEqual(result['core']['measured_seconds'], 10)

    def test_daily_boundaries_split_time_correctly(self):
        self.start = datetime(2026, 10, 9, 23, 59, 50, tzinfo=timezone.utc)
        result = measured_availability([self.row(0, True, True)], self.start, self.start + timedelta(seconds=20))
        self.assertEqual([day['core']['measured_seconds'] for day in result['history']], [10, 10])


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.tmp.name) / 'status.db'))
        self.database.initialize()
        self.repository = MonitorRepository(self.database)
        class Client:
            def list_containers(client):
                return [DockerContainer('monkee', 'running', 'healthy', None, sanitize_health(snapshot('degraded')))]
        self.checker = DockerChecker(Client())
        registry = CheckerRegistry([HttpChecker(), TcpChecker(), self.checker])
        self.service = MonitorService(self.repository, registry, self.checker)
        self.ident = self.repository.create(MonitorConfig('Monkee', '', 'docker', 'monkee', 60, 5, 3, True))

    def tearDown(self):
        self.tmp.cleanup()

    def test_detail_flows_into_existing_monitor_history_public_api_and_survives_restart(self):
        self.service.check_due()
        item = self.service.public_status()['monitors'][0]
        self.assertEqual(item['status'], 'degraded')
        self.assertEqual(item['application_health']['components']['gameApi']['status'], 'down')
        history = self.repository.history(self.ident)
        self.assertEqual(history[0]['core_available'], 1)
        self.assertEqual(history[0]['full_available'], 0)
        Database(self.database.path).initialize()
        reopened = MonitorRepository(Database(self.database.path))
        self.assertEqual(reopened.history(self.ident), history)

    def test_stale_detail_falls_back_to_docker_without_claiming_full_application_health(self):
        detail = sanitize_health(snapshot(timestamp='2026-01-01T00:00:00Z'))
        result = self.checker.state_result(DockerContainer('monkee', 'running', 'healthy', None, detail))
        self.assertEqual(result.status, 'operational')
        self.assertIsNone(result.core_available)
        self.assertIsNone(result.full_available)

    def test_stopped_container_overrides_cached_application_success(self):
        result = self.checker.state_result(DockerContainer('monkee', 'exited', 'healthy', None, snapshot()))
        self.assertEqual(result.status, 'down')
        self.assertFalse(result.core_available)

    def test_missing_detail_from_known_publisher_does_not_manufacture_success(self):
        self.service.check_due()
        monitor = self.repository.get(self.ident)
        self.service.record_result(monitor, self.checker.state_result(DockerContainer('monkee', 'running', 'healthy', None)))
        row = self.repository.history(self.ident)[0]
        self.assertIsNone(row['core_available'])
        self.assertIsNone(row['full_available'])
        public = self.service.public_status()['monitors'][0]
        self.assertEqual(public['status'], 'operational')
        self.assertEqual(public['application_health']['status'], 'unknown')

    def test_unknown_optional_evidence_keeps_explicit_core_measurement(self):
        detail = snapshot('unknown')
        detail['components']['gameApi']['status'] = 'unknown'
        result = self.checker.state_result(DockerContainer('monkee', 'running', 'healthy', None, sanitize_health(detail)))
        self.assertEqual(result.status, 'unknown')
        self.assertTrue(result.core_available)
        self.assertIsNone(result.full_available)

    def test_new_outage_after_monitoring_gap_is_a_separate_observed_incident(self):
        monitor = self.repository.get(self.ident)
        detail = sanitize_health(snapshot('degraded'))
        result = self.checker.state_result(DockerContainer('monkee', 'running', 'healthy', None, detail))
        with patch('status_server.service.utc_now', return_value='2026-01-01T00:00:00+00:00'):
            self.service.record_result(monitor, result)
        with patch('status_server.service.utc_now', return_value='2026-01-01T00:10:00+00:00'):
            self.service.record_result(monitor, result)
        incidents = self.repository.incidents(10)
        self.assertEqual(len(incidents), 2)
        ended = next(item for item in incidents if item['ended_at'])
        self.assertEqual(ended['ended_reason'], 'monitoring gap')
        self.assertEqual(ended['ended_at'], '2026-01-01T00:02:00+00:00')

    def test_stalled_monitoring_worker_cannot_leave_public_status_operational(self):
        self.service.check_due()
        with self.database.connection() as db:
            db.execute("UPDATE monitors SET last_checked='2026-01-01T00:00:00+00:00' WHERE id=?", (self.ident,))
        item = self.service.public_status()['monitors'][0]
        self.assertEqual(item['status'], 'unknown')
        self.assertTrue(item['monitoring_stale'])

    def test_application_expiry_is_enforced_between_status_server_polls(self):
        self.service.check_due()
        with patch('status_server.service.fresh_health', return_value=False):
            item = self.service.public_status()['monitors'][0]
        self.assertEqual(item['status'], 'unknown')
        self.assertEqual(item['application_health']['status'], 'unknown')

    def test_proxy_loss_is_unknown_and_preserves_history(self):
        self.service.check_due()
        with patch.object(self.checker.client, 'list_containers', side_effect=DockerUnavailable):
            self.service.record_result(self.repository.get(self.ident),
                                       self.checker.check_many([self.repository.get(self.ident)])[self.ident])
        self.assertEqual(self.repository.get(self.ident).status, 'unknown')
        self.assertEqual(len(self.repository.history(self.ident)), 2)

    def test_consumer_revalidates_details_without_breaking_ordinary_containers(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit):
                return json.dumps({'containers': [
                    {'name': 'ordinary', 'state': 'running', 'health': 'none'},
                    {'name': 'monkee', 'state': 'running', 'health': 'healthy', 'application_health': snapshot('degraded')}
                ]}).encode()
        client = DockerProxyClient('http://unused:8090/containers')
        with patch.object(client.opener, 'open', return_value=Response()):
            containers = client.list_containers()
        self.assertIsNone(containers[0].application_health)
        self.assertEqual(self.checker.state_result(containers[0]).status, 'operational')
        self.assertEqual(self.checker.state_result(containers[1]).status, 'degraded')


if __name__ == '__main__':
    unittest.main()
