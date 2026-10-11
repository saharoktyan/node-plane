"""Run separately against disposable PostgreSQL; no Telegram sends."""
import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

from backend.delivery_wakeup import DeliveryWakeup
from db.postgres_db import worker_connections
from tests.test_worker_postgres import WorkerPostgresTests


@unittest.skipUnless(os.getenv('NODE_PLANE_TEST_POSTGRES_DSN'), 'requires disposable PostgreSQL')
class DeliveryPostgresTests(unittest.TestCase):
    cleanup = WorkerPostgresTests.cleanup

    def setUp(self):
        WorkerPostgresTests.setUp(self)
        with self.db.transaction() as conn:
            conn.execute('CREATE TABLE backend_alert_deliveries (id INTEGER PRIMARY KEY)')
        self.db.reuse_connections = True
        self.db.max_idle = 8

    def wait_until(self, predicate):
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail('listener did not become ready')

    def test_worker_delivery_commit_wakes_api_but_rollback_does_not(self):
        wake = DeliveryWakeup(self.db.dsn)
        wake.start()
        self.addCleanup(wake.close)
        self.wait_until(lambda: wake.snapshot() > 0)
        generation = wake.snapshot()
        with self.assertRaises(ValueError):
            with worker_connections(), self.db.transaction() as conn:
                conn.execute('INSERT INTO backend_alert_deliveries VALUES (1)')
                raise ValueError('rollback')
        self.assertFalse(wake.wait(generation, 0.1))
        with worker_connections(), self.db.transaction() as conn:
            conn.execute('INSERT INTO backend_alert_deliveries VALUES (2)')
        self.assertTrue(wake.wait(generation, 2))

    def test_idle_api_connections_reused_across_threads_and_closed_on_shutdown(self):
        with patch.object(self.db, '_open', wraps=self.db._open) as opened:
            for _ in range(20):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    def query():
                        with self.db.connect() as conn:
                            return conn.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid']
                    pid = pool.submit(query).result()
                    if opened.call_count == 1:
                        first = pid
                    self.assertEqual(pid, first)
            self.assertEqual(opened.call_count, 1)
        retained = list(self.db._idle)
        self.db.close()
        self.assertTrue(all(conn._conn.closed for conn in retained))

    def test_listener_reconnects_after_connection_is_terminated(self):
        from psycopg.conninfo import make_conninfo
        identity = 'delivery_test_' + uuid4().hex
        wake = DeliveryWakeup(make_conninfo(self.db.dsn, application_name=identity))
        wake.start()
        self.addCleanup(wake.close)
        self.wait_until(lambda: wake.snapshot() > 0)
        def pid():
            with self.base.connect() as conn:
                row = conn.execute('SELECT pid FROM pg_stat_activity WHERE application_name=?',
                    (identity,)).fetchone()
                return row['pid'] if row else None
        original = pid()
        self.assertIsNotNone(original)
        with self.base.transaction() as conn:
            conn.execute('SELECT pg_terminate_backend(?)', (original,))
        self.wait_until(lambda: pid() is not None and pid() != original)
        generation = wake.snapshot()
        with self.db.transaction() as conn:
            conn.execute('INSERT INTO backend_alert_deliveries VALUES (3)')
        self.assertTrue(wake.wait(generation, 2))


@unittest.skipUnless(os.getenv('NODE_PLANE_TEST_POSTGRES_DSN'), 'requires disposable PostgreSQL')
class DeliveryHTTPPostgresTests(unittest.TestCase):
    def setUp(self):
        from tests.test_backups_postgres import BackupsPostgresTests
        self.drop_schema = lambda: BackupsPostgresTests.drop_schema(self)
        BackupsPostgresTests.setUp(self)
        self.addCleanup(self.db.close)

    def test_long_poll_wakes_on_announcement_and_claim_ack_stay_idempotent(self):
        from fastapi.testclient import TestClient
        from backend.http_api import create_app
        from backend.admin_cli import bootstrap_admin
        from backend.identity_repository import SQLIdentityRepository
        from backend.announcements import AnnouncementService
        from backend.credentials import CredentialService, ADAPTER_SCOPES
        from backend.authorization import PrincipalKind
        bootstrap_admin(SQLIdentityRepository(self.db), 102, self.db)
        credentials = CredentialService(self.db)
        token_id, token = credentials.issue(PrincipalKind.ADAPTER, ADAPTER_SCOPES)
        headers = {'Authorization': 'Bearer ' + token}
        announcement = AnnouncementService(self.db)
        entered = threading.Event()
        original = DeliveryWakeup.wait
        def waiting(wake, generation, timeout):
            entered.set()
            return original(wake, generation, timeout)
        with TestClient(create_app(self.db)) as client, patch.object(DeliveryWakeup, 'wait', waiting):
            deadline = time.monotonic() + 3
            while client.app.state.delivery_wakeup.snapshot() == 0:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            with ThreadPoolExecutor(max_workers=1) as pool:
                waiting_response = pool.submit(client.get,
                    '/api/v1/integrations/telegram/deliveries/wait', headers=headers)
                self.assertTrue(entered.wait(3))
                started = time.monotonic()
                announcement.queue(self.actor, 'Test notice', str(uuid4()))
                self.assertEqual(waiting_response.result(timeout=3).status_code, 200)
                self.assertLess(time.monotonic() - started, 3)
            claim_headers = {**headers, 'Idempotency-Key': str(uuid4())}
            endpoint = '/api/v1/integrations/telegram/announcements/claim'
            first = client.post(endpoint, headers=claim_headers).json()['delivery']
            self.assertIsNotNone(first)
            self.assertEqual(first, client.post(endpoint, headers=claim_headers).json()['delivery'])
            self.assertEqual(client.post(
                f"/api/v1/integrations/telegram/announcements/{first['id']}/ack",
                headers=claim_headers, json={'status': 'sent'}).status_code, 200)
            self.assertIsNone(client.post(endpoint, headers=claim_headers).json()['delivery'])
            credentials.revoke(token_id)
            self.assertEqual(client.post(endpoint, headers=claim_headers).status_code, 401)

