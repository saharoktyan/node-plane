"""Real monitoring concurrency: slow inspections cannot clear newer CPU alarms."""
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import TestCase, skipUnless
from unittest.mock import Mock, patch

from backend.alerts import AlertService
from backend.host_resources import monitor
from tests import test_backups_postgres as fixture


def cpu(value):
    return {'host_metrics': {'cpu_used_percent':value,'cpu_sample_window_seconds':60,'cpu_sample_age_seconds':0}}


@skipUnless(os.environ.get('NODE_PLANE_TEST_POSTGRES_DSN'), 'requires disposable PostgreSQL')
class CpuMonitoringPostgresTests(TestCase):
    drop_schema = fixture.BackupsPostgresTests.drop_schema

    def setUp(self):
        fixture.BackupsPostgresTests.setUp(self)
        self.addCleanup(self.db.close)
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_nodes(key,title,region,protocols_json,xray_transports_json,applied_revision) VALUES ('cpu-node','CPU node','EU','[]','[]',1)")
        self.driver = SimpleNamespace(binary_info=Mock(return_value={}),host_metrics=Mock(return_value=cpu(96)),inspect_node_services=Mock())
        self.alerts = AlertService(self.db,self.driver)
        self.alerts.preferences(self.actor, {'enabled':True})

    def test_late_full_scan_cannot_resolve_a_newer_fast_alarm(self):
        entered, release = threading.Event(), threading.Event()
        def inspect(key):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('fixture not released')
            return cpu(10)
        self.driver.inspect_node_services.side_effect = inspect
        start = datetime.now(timezone.utc)
        with patch('backend.alerts.datetime') as clock, ThreadPoolExecutor(max_workers=1) as pool:
            clock.fromisoformat = datetime.fromisoformat
            clock.now.return_value = start-timedelta(seconds=30)
            self.assertTrue(self.alerts.scheduled(cpu_only=True))
            clock.now.return_value = start
            full = pool.submit(self.alerts.scheduled)
            try:
                self.assertTrue(entered.wait(3))
                clock.now.return_value = start+timedelta(seconds=30)
                self.assertTrue(self.alerts.scheduled(cpu_only=True))
            finally:
                release.set()
            self.assertTrue(full.result(5))
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM backend_alert_state WHERE kind='cpu_high'").fetchone()['n'],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM backend_alert_events WHERE resolved=1").fetchone()['n'],0)

    def test_late_full_scan_cannot_recreate_an_alarm_after_newer_recovery(self):
        entered, release = threading.Event(), threading.Event()
        def inspect(key):
            entered.set()
            release.wait(5)
            return cpu(99)
        self.driver.inspect_node_services.side_effect = inspect
        start = datetime.now(timezone.utc)
        with patch('backend.alerts.datetime') as clock, ThreadPoolExecutor(max_workers=1) as pool:
            clock.fromisoformat = datetime.fromisoformat
            clock.now.return_value = start-timedelta(seconds=30)
            self.assertTrue(self.alerts.scheduled(cpu_only=True))
            clock.now.return_value = start
            full = pool.submit(self.alerts.scheduled)
            try:
                self.assertTrue(entered.wait(3))
                self.driver.host_metrics.return_value = cpu(20)
                clock.now.return_value = start+timedelta(seconds=30)
                self.assertTrue(self.alerts.scheduled(cpu_only=True))
                clock.now.return_value = start+timedelta(seconds=60)
                self.assertTrue(self.alerts.scheduled(cpu_only=True))
            finally:
                release.set()
            self.assertTrue(full.result(5))
        self.assertEqual(self.alerts.overview(self.actor)['active_count'],0)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM backend_alert_events WHERE kind='cpu_high'").fetchone()['n'],2)

    def test_cpu_collection_continues_while_remote_cpu_read_is_blocked(self):
        entered, release, stop = threading.Event(), threading.Event(), threading.Event()
        def inspect(key):
            entered.set()
            release.wait(5)
            return cpu(20)
        self.driver.host_metrics.side_effect = inspect
        thread = threading.Thread(target=monitor,args=(self.db,self.driver,stop),daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            deadline = time.monotonic()+3
            while True:
                with self.db.connect() as conn:
                    row = conn.execute("SELECT value FROM backend_system_settings WHERE key='controller_cpu_sample'").fetchone()
                if row:
                    break
                if time.monotonic() >= deadline:
                    self.fail('Slow remote inspection blocked independent controller CPU collection')
                time.sleep(.02)
            self.assertIn('host_metrics',json.loads(row['value']))
            self.assertFalse(release.is_set())
        finally:
            stop.set()
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_parallel_due_reminder_commits_only_once(self):
        self.alerts.preferences(self.actor, {'repeat_minutes': 30})
        start = datetime.now(timezone.utc)
        barrier = threading.Barrier(2)
        def inspect(key):
            barrier.wait(5)
            return cpu(96)
        with patch('backend.alerts.datetime') as clock:
            clock.fromisoformat = datetime.fromisoformat
            clock.now.return_value = start
            self.assertTrue(self.alerts.scheduled(cpu_only=True))
            clock.now.return_value = start + timedelta(minutes=30)
            self.driver.inspect_node_services.side_effect = inspect
            with ThreadPoolExecutor(max_workers=2) as pool:
                jobs = [pool.submit(self.alerts.scheduled) for _ in range(2)]
                self.assertEqual(sorted(job.result(10) for job in jobs), [False, True])
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM backend_alert_events WHERE kind='cpu_high'").fetchone()['n'], 2)

    def test_older_fast_scan_cannot_recreate_la_or_ram_after_newer_full_recovery(self):
        entered, release = threading.Event(), threading.Event()
        high = {'host_metrics': {'ram_used_percent': 95, 'load1': 5, 'cpus': 2}}
        healthy = {'host_metrics': {'ram_used_percent': 40, 'load1': 0.1, 'cpus': 2}}
        self.driver.host_metrics.return_value = high
        start = datetime.now(timezone.utc)
        def inspect(key):
            entered.set()
            release.wait(5)
            return high
        with patch('backend.alerts.datetime') as clock, ThreadPoolExecutor(max_workers=1) as pool:
            clock.fromisoformat = datetime.fromisoformat
            clock.now.return_value = start - timedelta(seconds=30)
            self.assertTrue(self.alerts.scheduled(cpu_only=True))
            self.driver.host_metrics.side_effect = inspect
            clock.now.return_value = start
            old = pool.submit(self.alerts.scheduled, cpu_only=True)
            try:
                self.assertTrue(entered.wait(3))
                self.driver.inspect_node_services.return_value = healthy
                for seconds in (30, 60):
                    clock.now.return_value = start + timedelta(seconds=seconds)
                    self.assertTrue(self.alerts.scheduled())
            finally:
                release.set()
            self.assertTrue(old.result(5))
        self.assertEqual(self.alerts.overview(self.actor)['active_count'], 0)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM backend_alert_events WHERE resolved=1").fetchone()['n'], 2)

    def test_late_failed_inspection_does_not_reopen_outage_after_fast_recovery(self):
        import grpc
        class Offline(grpc.RpcError):
            def code(self): return grpc.StatusCode.UNAVAILABLE
        entered, release = threading.Event(), threading.Event()
        slow_thread = [None]
        def slow_inspection(key):
            slow_thread[0] = threading.get_ident()
            entered.set()
            release.wait(5)
            raise Offline()
        self.driver.inspect_node_services.side_effect = Offline()
        self.driver.host_metrics.side_effect = Offline()
        start = datetime.now(timezone.utc)
        with patch('backend.alerts.datetime') as clock, ThreadPoolExecutor(max_workers=1) as pool:
            clock.fromisoformat = datetime.fromisoformat
            for seconds in (0, 30):
                clock.now.return_value = start + timedelta(seconds=seconds)
                self.alerts.scheduled()
            self.driver.inspect_node_services.side_effect = slow_inspection
            def metrics(key):
                if threading.get_ident() == slow_thread[0]: raise Offline()
                return cpu(20)
            self.driver.host_metrics.side_effect = metrics
            clock.now.return_value = start + timedelta(seconds=60)
            old = pool.submit(self.alerts.scheduled)
            try:
                self.assertTrue(entered.wait(3))
                for seconds in (90, 120):
                    clock.now.return_value = start + timedelta(seconds=seconds)
                    self.assertTrue(self.alerts.scheduled(cpu_only=True))
            finally:
                release.set()
            self.assertTrue(old.result(5))
        self.assertEqual(self.alerts.overview(self.actor)['active_count'], 0)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM backend_alert_events WHERE kind='node_unreachable'").fetchone()['n'], 2)

    def test_newer_unknown_fast_scan_cannot_hide_confirmed_network_failure(self):
        import grpc
        class Offline(grpc.RpcError):
            def code(self): return grpc.StatusCode.UNAVAILABLE
        entered, release = threading.Event(), threading.Event()
        def slow_inspection(key):
            entered.set()
            release.wait(5)
            raise Offline()
        self.driver.inspect_node_services.side_effect = Offline()
        self.driver.host_metrics.side_effect = Offline()
        start = datetime.now(timezone.utc)
        with patch('backend.alerts.datetime') as clock, ThreadPoolExecutor(max_workers=1) as pool:
            clock.fromisoformat = datetime.fromisoformat
            clock.now.return_value = start
            self.alerts.scheduled()
            self.driver.inspect_node_services.side_effect = slow_inspection
            clock.now.return_value = start + timedelta(seconds=30)
            old = pool.submit(self.alerts.scheduled)
            try:
                self.assertTrue(entered.wait(3))
                clock.now.return_value = start + timedelta(seconds=60)
                self.assertTrue(self.alerts.scheduled(cpu_only=True))
            finally:
                release.set()
            self.assertTrue(old.result(5))
        self.assertEqual([r['kind'] for r in self.alerts.overview(self.actor)['active']], ['node_unreachable'])
