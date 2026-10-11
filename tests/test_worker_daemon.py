import fcntl
import tempfile
import threading
import unittest
from unittest.mock import Mock

from backend.executor import run_daemon
from db.postgres_db import PostgresConnectionAdapter, PostgresDB, worker_connections


class WorkerDaemonTests(unittest.TestCase):
    def test_recovers_once_and_releases_mutation_lock_between_wakeups(self):
        stop = threading.Event()
        worker = Mock()
        clock = Mock(return_value=0)
        with tempfile.NamedTemporaryFile() as lock, open(lock.name) as contender:
            rounds = []
            def wait(timeout):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(contender, fcntl.LOCK_UN)
                rounds.append(timeout)
                if len(rounds) == 3:
                    stop.set()
            run_daemon(worker, lock.fileno(), stop, wait, clock)
        worker.recover.assert_called_once()
        worker.cycle.assert_called_once_with(stop)
        self.assertEqual(worker.drain.call_count, 2)

    def test_periodic_scan_without_notifications(self):
        stop = threading.Event()
        worker = Mock()
        now = [0]
        def wait(timeout):
            now[0] += timeout
            if now[0] >= 15:
                stop.set()
        with tempfile.NamedTemporaryFile() as lock:
            run_daemon(worker, lock.fileno(), stop, wait, lambda: now[0])
        self.assertEqual(worker.cycle.call_count, 3)
        worker.recover.assert_called_once()

    def test_cycle_failure_releases_lock_and_exits_for_systemd_recovery(self):
        worker = Mock()
        worker.cycle.side_effect = RuntimeError('database unavailable')
        with tempfile.NamedTemporaryFile() as lock, open(lock.name) as contender:
            with self.assertRaises(RuntimeError):
                run_daemon(worker, lock.fileno(), threading.Event(), Mock())
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(contender, fcntl.LOCK_UN)


class WorkerConnectionTests(unittest.TestCase):
    def test_reuse_keeps_transactions_separate_and_discards_failed_rollback(self):
        db = PostgresDB('test')
        raw = Mock(closed=False)
        adapter = PostgresConnectionAdapter(raw)
        db._open = Mock(return_value=adapter)
        with worker_connections():
            with db.connect():
                pass
            with self.assertRaises(ValueError):
                with db.transaction():
                    raise ValueError('rollback')
            with db.connect():
                pass
            self.assertEqual(db._open.call_count, 1)
            self.assertEqual(raw.commit.call_count, 2)
            raw.rollback.assert_called_once()
            raw.rollback.side_effect = OSError('disconnected')
            with self.assertRaises(ValueError):
                with db.transaction():
                    raise ValueError('original failure')
        raw.close.assert_called_once()

    def test_notifications_commit_with_queue_changes_and_rollback_clears_them(self):
        raw = Mock()
        raw.execute.return_value.rowcount = 1
        conn = PostgresConnectionAdapter(raw)
        conn.execute('INSERT INTO backend_node_jobs VALUES (?)', ('id',))
        conn.rollback()
        conn.commit()
        self.assertFalse(any('pg_notify' in call.args[0] for call in raw.execute.call_args_list))
        conn.execute('INSERT INTO backend_node_jobs VALUES (?)', ('id',))
        conn.commit()
        self.assertEqual(sum('pg_notify' in call.args[0] for call in raw.execute.call_args_list), 1)
        with worker_connections():
            conn.execute('UPDATE backend_node_jobs SET status=?', ('done',))
            conn.commit()
        self.assertEqual(sum('pg_notify' in call.args[0] for call in raw.execute.call_args_list), 1)
