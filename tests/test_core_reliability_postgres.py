"""Run separately against disposable PostgreSQL; no VPS or real driver calls."""
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from backend.authorization import AccessDenied
from backend.backups import BackupService, encoded
from backend.executor import IntentExecutor
from backend.node_lifecycle import NodeLifecycle
from backend.profile_commands import ProfileCommands
from backend.profiles import ProfileRepository
from tests import test_backups_postgres as fixture


class Crash(BaseException):
    """A terminated process bypasses the normal RPC error handler."""


class JournalDriver:
    def __init__(self):
        self.calls = []
        self.journal = {}
        self.lock = threading.Lock()

    def execute(self, task_id, intent):
        with self.lock:
            self.calls.append((task_id, intent))
            result = {'succeeded': True, 'driver_operation_id': task_id, 'result_json': '{}'}
            self.journal[task_id] = result
        return result

    def lookup(self, task_id, intent):
        return self.journal.get(task_id, {'succeeded': False})


class HookDatabase:
    backend_name = 'postgres'

    def __init__(self, db, hook):
        self.db, self.hook = db, hook

    @contextmanager
    def transaction(self):
        with self.db.transaction() as conn:
            hook = self.hook

            class Connection:
                def execute(self, sql, params=None):
                    result = conn.execute(sql, params)
                    hook(sql)
                    return result

            yield Connection()

    def connect(self):
        return self.db.connect()


@unittest.skipUnless(os.environ.get('NODE_PLANE_TEST_POSTGRES_DSN'), 'requires disposable PostgreSQL')
class CoreReliabilityPostgresTests(unittest.TestCase):
    setUp = fixture.BackupsPostgresTests.setUp
    drop_schema = fixture.BackupsPostgresTests.drop_schema

    def create_target(self):
        profile = ProfileRepository(self.db).create_profile(runtime_name='alice', display_name='Alice')
        with self.db.transaction() as conn:
            conn.execute("""INSERT INTO backend_nodes(key,title,region,protocols_json)
                VALUES ('node','Test node','Europe','["awg","xray"]')""")
        return profile

    def grant(self, profile, revision=1, key=None):
        return ProfileCommands(self.db).execute(self.actor, key or str(uuid4()),
            action='grants', profile_id=profile, revision=revision,
            values={'grants': [{'node_key': 'node', 'protocol': 'awg'}]})

    def race(self, first, second):
        start = threading.Barrier(2)

        def run(callback):
            start.wait(timeout=5)
            try:
                return callback()
            except AccessDenied as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(run, first), pool.submit(run, second)
            return a.result(timeout=15), b.result(timeout=15)

    def test_competing_profile_revisions_have_one_winner(self):
        profile = self.create_target()

        def edit(name):
            return lambda: ProfileCommands(self.db).execute(self.actor, str(uuid4()),
                action='edit', profile_id=profile, revision=1, values={'display_name': name})

        results = self.race(edit('First'), edit('Second'))
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertIn('revision_conflict', results)
        self.assertEqual(ProfileRepository(self.db).get(profile)['desired_revision'], 2)

    def test_concurrent_idempotent_command_creates_only_one_operation(self):
        profile = self.create_target()
        key = str(uuid4())
        a, b = self.race(lambda: self.grant(profile, key=key), lambda: self.grant(profile, key=key))
        self.assertEqual(a, b)
        with self.db.connect() as conn:
            self.assertEqual(len(conn.execute('SELECT id FROM backend_operations WHERE profile_id=?', (profile,)).fetchall()), 1)
        self.assertEqual(ProfileRepository(self.db).get(profile)['desired_revision'], 2)

    def test_two_executor_claims_dispatch_same_task_once(self):
        profile = self.create_target()
        self.grant(profile)
        selected = threading.Barrier(2)

        def hook(sql):
            if 'WHERE t.status' in sql and 'ORDER BY o.created_at' in sql:
                selected.wait(timeout=5)

        driver = JournalDriver()
        hooked = HookDatabase(self.db, hook)
        a, b = self.race(lambda: IntentExecutor(hooked, driver).run_one(),
                         lambda: IntentExecutor(hooked, driver).run_one())
        self.assertEqual(sorted((a, b)), [False, True])
        self.assertEqual(len(driver.calls), 1)

    def test_inflight_task_blocks_other_profile_on_same_node(self):
        profile = self.create_target()
        other = ProfileRepository(self.db).create_profile(runtime_name='bob', display_name='Bob')
        self.grant(profile)
        self.grant(other)
        entered, release = threading.Event(), threading.Event()
        driver = JournalDriver()
        original = driver.execute

        def execute(task_id, intent):
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError('test did not release driver')
            return original(task_id, intent)

        driver.execute = execute
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(IntentExecutor(self.db, driver).run_one)
            try:
                self.assertTrue(entered.wait(timeout=5))
                self.assertFalse(IntentExecutor(self.db, driver).run_one())
            finally:
                release.set()
            self.assertTrue(future.result(timeout=5))
        self.assertTrue(IntentExecutor(self.db, driver).run_one())
        self.assertEqual(len(driver.calls), 2)

    def test_grant_racing_with_drain_never_leaves_access_or_dispatches_ensure(self):
        profile = self.create_target()
        self.race(lambda: self.grant(profile), lambda: NodeLifecycle(self.db).start_drain(self.actor, 'node'))
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT enabled FROM backend_nodes WHERE key='node'").fetchone()['enabled'], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) AS count FROM backend_grants WHERE node_key='node'").fetchone()['count'], 0)
        driver = JournalDriver()
        worker = IntentExecutor(self.db, driver)
        while worker.run_one():
            pass
        self.assertTrue(all(intent['action'] == 'delete' for _, intent in driver.calls))
        self.assertTrue(NodeLifecycle(self.db).drain_status(self.actor, 'node')['revocations_complete'])

    def test_drain_waits_for_profile_edit_before_locking_node(self):
        profile = self.create_target()
        self.grant(profile)
        locked, release, drain_started, node_locked = (threading.Event() for _ in range(4))

        def editing(sql):
            if sql.startswith('UPDATE backend_profiles SET display_name'):
                locked.set()
                if not release.wait(timeout=5):
                    raise RuntimeError('test did not release edit')

        def draining(sql):
            if sql.startswith('UPDATE backend_nodes SET enabled = 0'):
                node_locked.set()

        def drain():
            drain_started.set()
            return NodeLifecycle(HookDatabase(self.db, draining)).start_drain(self.actor, 'node')

        with ThreadPoolExecutor(max_workers=2) as pool:
            edit = pool.submit(ProfileCommands(HookDatabase(self.db, editing)).execute,
                self.actor, str(uuid4()), action='edit', profile_id=profile, revision=2,
                values={'display_name': 'Edited'})
            self.assertTrue(locked.wait(timeout=5))
            removal = pool.submit(drain)
            try:
                self.assertTrue(drain_started.wait(timeout=5))
                self.assertFalse(node_locked.wait(timeout=0.3), 'drain inverted the profile/node lock order')
            finally:
                release.set()
            edit.result(timeout=5)
            removal.result(timeout=5)
        self.assertEqual(ProfileRepository(self.db).get(profile)['display_name'], 'Edited')
        self.assertEqual(ProfileRepository(self.db).get(profile)['desired_revision'], 4)

    def test_crash_before_dispatch_stays_blocked_without_remote_replay(self):
        profile = self.create_target()
        self.grant(profile)
        driver = JournalDriver()

        def crash(task_id, intent):
            raise Crash()

        driver.execute = crash
        worker = IntentExecutor(self.db, driver)
        with self.assertRaises(Crash):
            worker.run_one()
        worker.recover()
        self.assertEqual(worker.reconcile_completed(), 0)
        self.assertFalse(worker.run_one())
        self.assertEqual(driver.calls, [])
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT status FROM backend_operation_tasks').fetchone()['status'], 'blocked')

    def test_crash_after_remote_success_recovers_by_lookup_without_replay(self):
        profile = self.create_target()
        self.grant(profile)
        driver = JournalDriver()
        original = driver.execute

        def crash(task_id, intent):
            original(task_id, intent)
            raise Crash()

        driver.execute = crash
        worker = IntentExecutor(self.db, driver)
        with self.assertRaises(Crash):
            worker.run_one()
        worker.recover()
        self.assertEqual(worker.reconcile_completed(), 1)
        self.assertEqual(worker.reconcile_completed(), 0)
        self.assertFalse(worker.run_one())
        self.assertEqual(len(driver.calls), 1)

    def test_result_transaction_failure_recovers_durable_success_without_replay(self):
        profile = self.create_target()
        self.grant(profile)
        driver = JournalDriver()

        def fail_result(sql):
            if 'SET status = ?, driver_operation_id' in sql:
                raise RuntimeError('simulated transaction failure')

        worker = IntentExecutor(HookDatabase(self.db, fail_result), driver)
        with self.assertRaises(RuntimeError):
            worker.run_one()
        recovered = IntentExecutor(self.db, driver)
        recovered.recover()
        self.assertEqual(recovered.reconcile_completed(), 1)
        self.assertFalse(recovered.run_one())
        self.assertEqual(len(driver.calls), 1)

    def test_missing_or_failed_journal_result_does_not_allow_later_revocation(self):
        profile = self.create_target()
        self.grant(profile)
        driver = JournalDriver()

        def timeout(task_id, intent):
            raise TimeoutError('unconfirmed outcome')

        driver.execute = timeout
        worker = IntentExecutor(self.db, driver)
        self.assertTrue(worker.run_one())
        ProfileCommands(self.db).execute(self.actor, str(uuid4()), action='grants',
            profile_id=profile, revision=2, values={'grants': []})
        self.assertEqual(worker.reconcile_completed(), 0)
        self.assertFalse(worker.run_one())

    def test_restore_admission_blocks_profile_command_inside_transaction(self):
        profile = self.create_target()
        backup_id = self.service.create_snapshot()['backup_id']
        checksum = self.service.detail(self.actor, backup_id)['checksum']
        self.service.queue(self.actor, str(uuid4()), 'restore', backup_id, checksum)
        with self.assertRaises(AccessDenied) as denied:
            ProfileCommands(self.db).execute(self.actor, str(uuid4()), action='edit',
                profile_id=profile, revision=1, values={'display_name': 'Forbidden'})
        self.assertEqual(denied.exception.code, 'restore_in_progress')
        self.assertEqual(ProfileRepository(self.db).get(profile)['display_name'], 'Alice')
        with self.assertRaises(AccessDenied) as denied:
            self.service.preferences(self.actor, {'enabled': True})
        self.assertEqual(denied.exception.code, 'restore_in_progress')

    def test_restore_admission_serializes_a_concurrent_profile_command(self):
        profile = self.create_target()
        backup_id = self.service.create_snapshot()['backup_id']
        checksum = self.service.detail(self.actor, backup_id)['checksum']
        admitted, release, started = (threading.Event() for _ in range(3))
        def hold_restore(sql):
            if sql.startswith('INSERT INTO backend_backup_jobs'):
                admitted.set()
                if not release.wait(timeout=5):
                    raise RuntimeError('test did not release restore')
        restoring = BackupService(HookDatabase(self.db, hold_restore), self.service.root)
        def edit():
            started.set()
            try:
                return ProfileCommands(self.db).execute(self.actor, str(uuid4()),
                    action='edit', profile_id=profile, revision=1,
                    values={'display_name': 'Forbidden'})
            except AccessDenied as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            restore = pool.submit(restoring.queue, self.actor, str(uuid4()), 'restore', backup_id, checksum)
            self.assertTrue(admitted.wait(timeout=5))
            editing = pool.submit(edit)
            try:
                self.assertTrue(started.wait(timeout=5))
            finally:
                release.set()
            restore.result(timeout=5)
            self.assertEqual(editing.result(timeout=5), 'restore_in_progress')
        self.assertEqual(ProfileRepository(self.db).get(profile)['display_name'], 'Alice')

    def test_corrupt_snapshot_rejected_without_freezing_profiles(self):
        profile = self.create_target()
        backup_id = self.service.create_snapshot()['backup_id']
        checksum = self.service.detail(self.actor, backup_id)['checksum']
        path = self.service._path(backup_id)
        value = json.loads(path.read_text())
        value['tables']['backend_profiles']['rows'][0]['display_name'] = 'Tampered'
        path.write_text(json.dumps(value))
        with self.assertRaises(AccessDenied) as denied:
            self.service.queue(self.actor, str(uuid4()), 'restore', backup_id, checksum)
        self.assertEqual(denied.exception.code, 'backup_invalid')
        self.assertFalse(ProfileRepository(self.db).get(profile)['frozen'])
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) AS count FROM backend_backup_jobs').fetchone()['count'], 0)

    def test_restore_retry_is_idempotent_but_other_backup_is_rejected(self):
        self.create_target()
        backup_id = self.service.create_snapshot()['backup_id']
        checksum = self.service.detail(self.actor, backup_id)['checksum']
        key = str(uuid4())
        job = self.service.queue(self.actor, key, 'restore', backup_id, checksum)
        retry = self.service.queue(self.actor, key, 'restore', backup_id, checksum)
        self.assertEqual(retry['id'], job['id'])
        self.assertEqual(retry['status'], job['status'])
        with self.assertRaises(AccessDenied) as denied:
            self.service.queue(self.actor, str(uuid4()), 'create')
        self.assertEqual(denied.exception.code, 'backup_pending')

    def test_incompatible_schema_with_valid_checksum_is_rejected(self):
        profile = self.create_target()
        backup_id = self.service.create_snapshot()['backup_id']
        path = self.service._path(backup_id)
        value = json.loads(path.read_text())
        value['tables']['backend_profiles']['columns'].append('future_column')
        for row in value['tables']['backend_profiles']['rows']:
            row['future_column'] = None
        value['checksum'] = hashlib.sha256(encoded(value['tables'])).hexdigest()
        path.write_bytes(encoded(value))
        with self.assertRaises(AccessDenied) as denied:
            self.service.queue(self.actor, str(uuid4()), 'restore', backup_id, value['checksum'])
        self.assertEqual(denied.exception.code, 'backup_incompatible')
        self.assertFalse(ProfileRepository(self.db).get(profile)['frozen'])

    def test_failed_revocation_blocks_restore_and_preserves_frozen_current_state(self):
        profile = self.create_target()
        self.grant(profile)
        IntentExecutor(self.db, JournalDriver()).run_one()
        backup_id = self.service.create_snapshot()['backup_id']
        checksum = self.service.detail(self.actor, backup_id)['checksum']
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_profiles SET display_name='Current' WHERE id=?", (profile,))
        job = self.service.queue(self.actor, str(uuid4()), 'restore', backup_id, checksum)
        self.assertTrue(self.service.run_one())
        driver = JournalDriver()
        def timeout(task_id, intent):
            raise TimeoutError('unconfirmed revocation')
        driver.execute = timeout
        self.assertTrue(IntentExecutor(self.db, driver).run_one())
        self.assertTrue(self.service.run_one())
        failed = self.service.get(self.actor, job['id'])
        self.assertEqual(failed['status'], 'blocked')
        self.assertEqual(failed['result']['code'], 'backup_revocations_failed')
        current = ProfileRepository(self.db).get(profile)
        self.assertEqual(current['display_name'], 'Current')
        self.assertTrue(current['frozen'])

    def test_restore_transaction_failure_rolls_back_partial_replacement(self):
        profile = self.create_target()
        backup_id = self.service.create_snapshot()['backup_id']
        checksum = self.service.detail(self.actor, backup_id)['checksum']
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_profiles SET display_name='Current' WHERE id=?", (profile,))
        job = self.service.queue(self.actor, str(uuid4()), 'restore', backup_id, checksum)
        self.service.run_one()
        def fail_insert(sql):
            if sql.startswith('INSERT INTO backend_profiles ('):
                raise RuntimeError('simulated restore transaction failure')
        broken = BackupService(HookDatabase(self.db, fail_insert), self.service.root)
        self.assertTrue(broken.run_one())
        self.assertEqual(self.service.get(self.actor, job['id'])['status'], 'blocked')
        self.assertEqual(ProfileRepository(self.db).get(profile)['display_name'], 'Current')
        self.assertIsNotNone(fixture.SQLIdentityRepository(self.db).find_telegram_account(101))

    def test_crash_during_restore_rolls_back_and_next_worker_completes(self):
        profile = self.create_target()
        backup_id = self.service.create_snapshot()['backup_id']
        checksum = self.service.detail(self.actor, backup_id)['checksum']
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_profiles SET display_name='Current' WHERE id=?", (profile,))
        job = self.service.queue(self.actor, str(uuid4()), 'restore', backup_id, checksum)
        self.service.run_one()
        def crash_insert(sql):
            if sql.startswith('INSERT INTO backend_profiles ('):
                raise Crash()
        interrupted = BackupService(HookDatabase(self.db, crash_insert), self.service.root)
        with self.assertRaises(Crash):
            interrupted.run_one()
        self.assertEqual(ProfileRepository(self.db).get(profile)['display_name'], 'Current')
        self.assertEqual(self.service.get(self.actor, job['id'])['status'], 'running')
        self.assertTrue(self.service.run_one())
        self.assertEqual(ProfileRepository(self.db).get(profile)['display_name'], 'Alice')
        self.assertEqual(self.service.get(self.actor, job['id'])['status'], 'succeeded')

    def test_restore_with_bound_node_inventory_clears_old_host_binding(self):
        self.create_target()
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_node_removal_inventories VALUES ('node','{}')")
        backup_id = self.service.create_snapshot()['backup_id']
        checksum = self.service.detail(self.actor, backup_id)['checksum']
        job = self.service.queue(self.actor, str(uuid4()), 'restore', backup_id, checksum)
        self.service.run_one()
        self.service.run_one()
        self.assertEqual(self.service.get(self.actor, job['id'])['status'], 'succeeded')
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) AS count FROM backend_node_removal_inventories').fetchone()['count'], 0)
            self.assertEqual(conn.execute("SELECT enabled FROM backend_nodes WHERE key='node'").fetchone()['enabled'], 0)


class WorkerProcessLockTests(unittest.TestCase):
    def test_second_worker_refuses_shared_lock_before_opening_database(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / 'worker.lock'
            with lock_path.open('w') as stream:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                process = subprocess.run([sys.executable, '-m', 'backend.executor',
                    '--lock-file', str(lock_path)], capture_output=True, text=True,
                    timeout=10, env={**os.environ, 'POSTGRES_DSN': 'deliberately-invalid'})
                self.assertNotEqual(process.returncode, 0)
                self.assertIn('BlockingIOError', process.stderr)
                self.assertNotIn('psycopg', process.stderr)
