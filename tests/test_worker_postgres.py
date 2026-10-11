"""Integration checks against an explicitly supplied disposable PostgreSQL."""
import os
import fcntl
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from uuid import uuid4

from db.postgres_db import PostgresDB, worker_connections
from backend.worker_wakeup import Wakeup


@unittest.skipUnless(os.getenv('NODE_PLANE_TEST_POSTGRES_DSN'), 'requires disposable PostgreSQL')
class WorkerPostgresTests(unittest.TestCase):
    def setUp(self):
        from psycopg.conninfo import make_conninfo
        self.base = PostgresDB(os.environ['NODE_PLANE_TEST_POSTGRES_DSN'])
        self.schema = 'worker_test_' + uuid4().hex
        with self.base.transaction() as conn:
            conn.execute(f'CREATE SCHEMA {self.schema}')
        self.db = PostgresDB(make_conninfo(self.base.dsn, options=f'-c search_path={self.schema}'))
        with self.db.transaction() as conn:
            conn.execute('CREATE TABLE backend_node_jobs (id INTEGER PRIMARY KEY)')
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.db.close()
        with self.base.transaction() as conn:
            conn.execute(f'DROP SCHEMA {self.schema} CASCADE')
        self.base.close()

    def test_reuse_nested_transactions_and_rollback(self):
        with worker_connections():
            with self.db.transaction() as outer:
                pid = outer.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid']
                with self.db.transaction() as inner:
                    self.assertNotEqual(pid, inner.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid'])
            with self.db.transaction() as conn:
                self.assertEqual(pid, conn.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid'])
            with self.assertRaises(ValueError):
                with self.db.transaction() as conn:
                    conn.execute('INSERT INTO backend_node_jobs VALUES (1)')
                    raise ValueError('rollback')
            with self.db.connect() as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) AS n FROM backend_node_jobs').fetchone()['n'], 0)

    def test_only_committed_external_work_notifies_listener(self):
        import psycopg
        with psycopg.connect(self.base.dsn, autocommit=True) as listener:
            listener.execute('LISTEN node_plane_work')
            with self.assertRaises(ValueError):
                with self.db.transaction() as conn:
                    conn.execute('INSERT INTO backend_node_jobs VALUES (1)')
                    raise ValueError('rollback')
            with worker_connections(), self.db.transaction() as conn:
                conn.execute('INSERT INTO backend_node_jobs VALUES (2)')
            self.assertEqual(list(listener.notifies(timeout=0.1, stop_after=1)), [])
            with self.db.transaction() as conn:
                conn.execute('INSERT INTO backend_node_jobs VALUES (3)')
            self.assertEqual(len(list(listener.notifies(timeout=1, stop_after=1))), 1)

    def test_wait_wakes_before_periodic_deadline(self):
        with Wakeup(self.base.dsn) as wake:
            stop = threading.Event()
            wake.wait(0, stop)  # Establish LISTEN before submitting work.
            finished = threading.Event()
            thread = threading.Thread(target=lambda: (wake.wait(5, stop), finished.set()))
            thread.start()
            try:
                with self.db.transaction() as conn:
                    conn.execute('INSERT INTO backend_node_jobs VALUES (4)')
                self.assertTrue(finished.wait(2), 'notification did not wake worker')
            finally:
                stop.set()
                thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_daemon_lifetime_lock_stop_and_restart(self):
        from db.migrations import migrate
        # The queue-only fixture belongs to this test schema and is replaced
        # with the full production schema for the subprocess.
        with self.db.transaction() as conn:
            conn.execute('DROP TABLE backend_node_jobs')
        migrate(self.db)
        env = {**os.environ, 'POSTGRES_DSN': self.db.dsn, 'PYTHONPATH': 'app'}
        with tempfile.TemporaryDirectory() as directory:
            lock = str(Path(directory) / 'worker.lock')
            command = [sys.executable, '-m', 'backend.executor', '--lock-file', lock, '--daemon']
            for _ in range(2):
                worker = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        if worker.poll() is not None:
                            self.fail(worker.communicate()[1].decode())
                        with self.base.connect() as conn:
                            listening = conn.execute("SELECT COUNT(*) AS n FROM pg_stat_activity WHERE query = 'LISTEN node_plane_work'").fetchone()['n']
                        if listening:
                            break
                        time.sleep(0.05)
                    else:
                        self.fail('daemon did not become ready')
                    second = subprocess.run(command, env=env, capture_output=True, timeout=5)
                    self.assertNotEqual(second.returncode, 0)
                    with open(lock) as handle:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fcntl.flock(handle, fcntl.LOCK_UN)
                    worker.terminate()
                    output = worker.communicate(timeout=5)
                    self.assertEqual(worker.returncode, 0, output[1].decode())
                finally:
                    if worker.poll() is None:
                        worker.kill()
                        worker.communicate()
