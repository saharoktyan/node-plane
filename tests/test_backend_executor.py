import json
import unittest

from backend.executor import IntentExecutor
from backend.authorization import Actor, Principal, PrincipalKind
from tests import test_backend_operations


class FakeDriver:
    def __init__(self, failure=False):
        self.calls = []
        self.failure = failure

    def execute(self, task_id, intent):
        self.calls.append((task_id, intent))
        if self.failure:
            raise TimeoutError('private transport details')
        return {'driver_operation_id': task_id, 'succeeded': True, 'result_json': '{"private": "config"}'}


class BackendExecutorTests(unittest.TestCase):
    setUp = test_backend_operations.BackendOperationTests.setUp
    headers_for = test_backend_operations.BackendOperationTests.headers_for
    create = test_backend_operations.BackendOperationTests.create
    prepare = test_backend_operations.BackendOperationTests.prepare
    grants = test_backend_operations.BackendOperationTests.grants

    def actor(self, account=None):
        account = account or self.admin
        return Actor(Principal('local-repair', PrincipalKind.ACCOUNT,
            frozenset({'maintenance.manage'}), account.id), account)

    def test_admin_repair_supersedes_old_revisions_and_queues_current_state(self):
        profile = self.prepare()
        grant = [{'node_key': 'node', 'protocol': 'awg'}]
        first = self.grants(profile, 1, grant).json()
        self.grants(profile, 2, [])
        self.grants(profile, 3, grant)
        blocked_id = self.db.connection.execute("SELECT id FROM backend_operation_tasks WHERE operation_id = ?", (first['operation_id'],)).fetchone()[0]
        driver = FakeDriver(failure=True)
        worker = IntentExecutor(self.db, driver)
        self.assertTrue(worker.run_one())
        resolutions = []
        observation = {'disk_present': True, 'live_present': True,
                       'identity_matches': True, 'config_available': True}
        driver.resolve = lambda task_id, intent: resolutions.append((task_id, intent)) or observation
        repaired = worker.resolve_blocked(self.actor(), blocked_id)
        self.assertEqual(repaired['observation'], observation)
        self.assertEqual(worker.resolve_blocked(self.actor(), blocked_id), repaired)
        self.assertEqual(len(resolutions), 1)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM backend_operation_tasks WHERE status = 'superseded'").fetchone()[0], 3)
        self.assertEqual(self.db.connection.execute('SELECT desired_revision FROM backend_profiles WHERE id = ?', (profile,)).fetchone()[0], 5)
        driver.failure = False
        self.assertTrue(worker.run_one())
        self.assertEqual(driver.calls[-1][1]['action'], 'ensure')
        self.assertEqual(driver.calls[-1][1]['desired_revision'], 5)
        self.assertFalse(worker.run_one())

    def test_repair_requires_admin_and_preserves_block_on_remote_failure(self):
        profile = self.prepare()
        first = self.grants(profile, 1, [{'node_key': 'node', 'protocol': 'awg'}]).json()
        task_id = self.db.connection.execute('SELECT id FROM backend_operation_tasks WHERE operation_id = ?',
                                             (first['operation_id'],)).fetchone()[0]
        driver = FakeDriver(failure=True)
        worker = IntentExecutor(self.db, driver)
        worker.run_one()
        member = self.identities.resolve_telegram(102)
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_accounts SET status = 'approved' WHERE id = ?", (member.id,))
        with self.assertRaises(Exception):
            worker.resolve_blocked(self.actor(self.identities.get_account(member.id)), task_id)
        def unavailable(task_id, intent):
            raise TimeoutError('agent did not restart')
        driver.resolve = unavailable
        with self.assertRaises(TimeoutError):
            worker.resolve_blocked(self.actor(), task_id)
        self.assertEqual(self.db.connection.execute('SELECT status FROM backend_operation_tasks WHERE id = ?', (task_id,)).fetchone()[0], 'blocked')

    def test_repair_can_complete_after_backend_commit_failure(self):
        profile = self.prepare()
        first = self.grants(profile, 1, [{'node_key': 'node', 'protocol': 'awg'}]).json()
        task_id = self.db.connection.execute('SELECT id FROM backend_operation_tasks WHERE operation_id = ?',
                                             (first['operation_id'],)).fetchone()[0]
        driver = FakeDriver(failure=True)
        worker = IntentExecutor(self.db, driver)
        worker.run_one()
        observation = {'disk_present': False, 'live_present': False,
                       'identity_matches': False, 'config_available': False}
        calls = []
        driver.resolve = lambda key, intent: calls.append(key) or observation
        with self.db.transaction() as conn:
            conn.execute("""CREATE TRIGGER fail_repair BEFORE INSERT ON backend_repairs
                BEGIN SELECT RAISE(ABORT, 'temporary persistence fault'); END""")
        with self.assertRaises(Exception):
            worker.resolve_blocked(self.actor(), task_id)
        self.assertEqual(self.db.connection.execute('SELECT status FROM backend_operation_tasks WHERE id = ?', (task_id,)).fetchone()[0], 'blocked')
        self.assertEqual(self.db.connection.execute('SELECT desired_revision FROM backend_profiles WHERE id = ?', (profile,)).fetchone()[0], 2)
        with self.db.transaction() as conn:
            conn.execute('DROP TRIGGER fail_repair')
        repaired = worker.resolve_blocked(self.actor(), task_id)
        self.assertEqual(len(calls), 2)
        self.assertEqual(repaired['observation'], observation)
        self.assertEqual(self.db.connection.execute('SELECT desired_revision FROM backend_profiles WHERE id = ?', (profile,)).fetchone()[0], 3)

    def test_live_inspection_is_visible_but_does_not_unblock_uncertain_task(self):
        profile = self.prepare()
        result = self.grants(profile, 1, [{'node_key': 'node', 'protocol': 'awg'}]).json()
        driver = FakeDriver(failure=True)
        executor = IntentExecutor(self.db, driver)
        executor.run_one()
        driver.inspect = lambda intent: {'disk_present': True, 'live_present': True,
                                        'identity_matches': True, 'config_available': True}
        self.assertEqual(executor.inspect_blocked(), 1)
        response = self.client.get('/api/v1/operations/' + result['operation_id'], headers=self.headers_for()).json()
        self.assertEqual(response['status'], 'blocked')
        self.assertTrue(response['tasks'][0]['inspection']['available'])
        self.assertTrue(response['tasks'][0]['inspection']['identity_matches'])
        self.assertIsNotNone(response['tasks'][0]['inspected_at'])
        self.assertFalse(executor.run_one())
        self.assertEqual(len(driver.calls), 1)

        def unavailable(intent):
            raise TimeoutError('raw transport detail')

        driver.inspect = unavailable
        self.assertEqual(executor.inspect_blocked(), 0)
        raw = self.client.get('/api/v1/operations/' + result['operation_id'], headers=self.headers_for())
        self.assertFalse(raw.json()['tasks'][0]['inspection']['available'])
        self.assertIsNone(raw.json()['tasks'][0]['inspection']['live_present'])
        self.assertNotIn('raw transport detail', raw.text)

    def test_confirmed_success_recovers_without_repeating_remote_command(self):
        profile = self.prepare()
        first = self.grants(profile, 1, [{'node_key': 'node', 'protocol': 'awg'}]).json()
        self.grants(profile, 2, [])
        driver = FakeDriver(failure=True)
        executor = IntentExecutor(self.db, driver)
        executor.run_one()
        looked_up = []

        def lookup(task_id, intent):
            looked_up.append(task_id)
            return {'succeeded': True, 'driver_operation_id': 'confirmed', 'result_json': '{"secret":"config"}'}

        driver.lookup = lookup
        self.assertEqual(executor.reconcile_completed(), 1)
        self.assertEqual(len(driver.calls), 1)
        self.assertEqual(executor.reconcile_completed(), 0)
        result = self.client.get('/api/v1/operations/' + first['operation_id'], headers=self.headers_for())
        self.assertEqual(result.json()['status'], 'succeeded')
        self.assertNotIn('secret', result.text)
        driver.failure = False
        self.assertTrue(executor.run_one())
        self.assertEqual(driver.calls[-1][1]['action'], 'delete')

    def test_failed_missing_and_running_journal_entries_keep_node_blocked(self):
        profile = self.prepare()
        self.grants(profile, 1, [{'node_key': 'node', 'protocol': 'awg'}])
        self.grants(profile, 2, [])
        driver = FakeDriver(failure=True)
        executor = IntentExecutor(self.db, driver)
        executor.run_one()
        driver.lookup = lambda task_id, intent: {'succeeded': False}
        self.assertEqual(executor.reconcile_completed(), 0)
        self.assertFalse(executor.run_one())

        def unavailable(task_id, intent):
            raise TimeoutError('private lookup failure')

        driver.lookup = unavailable
        self.assertEqual(executor.reconcile_completed(), 0)
        self.assertFalse(executor.run_one())
        self.assertEqual(len(driver.calls), 1)

    def test_revision_order_stable_xray_identity_and_private_result(self):
        profile = self.prepare()
        grant = [{'node_key': 'node', 'protocol': 'xray'}]
        first = self.grants(profile, 1, grant).json()
        self.grants(profile, 2, [])
        self.grants(profile, 3, grant)
        driver = FakeDriver()
        executor = IntentExecutor(self.db, driver)
        executor.recover()
        while executor.run_one():
            pass
        self.assertEqual([intent['action'] for _, intent in driver.calls], ['ensure', 'delete', 'ensure'])
        self.assertEqual(driver.calls[0][1]['xray'], driver.calls[2][1]['xray'])
        self.assertEqual(len({key for key, _ in driver.calls}), 3)
        self.assertFalse(executor.run_one())
        result = self.client.get('/api/v1/operations/' + first['operation_id'], headers=self.headers_for())
        self.assertEqual(result.json()['status'], 'succeeded')
        self.assertNotIn('private', result.text)
        self.assertNotIn('xray_uuid', result.text)

    def test_timeout_blocks_later_revocation_without_blind_retry(self):
        profile = self.prepare()
        grant = [{'node_key': 'node', 'protocol': 'awg'}]
        first = self.grants(profile, 1, grant).json()
        self.grants(profile, 2, [])
        driver = FakeDriver(failure=True)
        executor = IntentExecutor(self.db, driver)
        self.assertTrue(executor.run_one())
        self.assertFalse(executor.run_one())
        self.assertEqual(len(driver.calls), 1)
        result = self.client.get('/api/v1/operations/' + first['operation_id'], headers=self.headers_for())
        self.assertEqual(result.json()['status'], 'blocked')
        self.assertNotIn('private transport', result.text)

    def test_crash_recovery_blocks_running_task_and_node(self):
        profile = self.prepare()
        self.grants(profile, 1, [{'node_key': 'node', 'protocol': 'awg'}])
        self.grants(profile, 2, [])
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_operation_tasks SET status = 'running' WHERE operation_id IN (SELECT id FROM backend_operations WHERE desired_revision = 2)")
            conn.execute("UPDATE backend_operations SET status = 'running' WHERE desired_revision = 2")
        executor = IntentExecutor(self.db, FakeDriver())
        executor.recover()
        self.assertFalse(executor.run_one())
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM backend_operation_tasks WHERE status = 'blocked'").fetchone()[0], 1)

    def test_expired_pending_ensure_is_never_dispatched(self):
        profile = self.prepare()
        self.grants(profile, 1, [{'node_key': 'node', 'protocol': 'awg'}])
        with self.db.transaction() as conn:
            row = conn.execute('SELECT id, intent_json FROM backend_operation_tasks').fetchone()
            intent = json.loads(row['intent_json'])
            intent['expires_at'] = '2000-01-01T00:00:00+00:00'
            conn.execute('UPDATE backend_operation_tasks SET intent_json = ? WHERE id = ?', (json.dumps(intent), row['id']))
        driver = FakeDriver()
        executor = IntentExecutor(self.db, driver)
        self.assertTrue(executor.run_one())
        self.assertEqual(driver.calls, [])
        self.assertFalse(executor.run_one())

    def test_expiry_queues_durable_revocation_once_and_extension_restores_access(self):
        from datetime import datetime, timedelta, timezone
        profile = self.prepare()
        self.db.connection.execute('UPDATE backend_profiles SET owner_account_id = NULL WHERE id = ?', (profile,))
        self.grants(profile, 1, [{'node_key': 'node', 'protocol': 'awg'}])
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        updated = self.client.patch(f'/api/v1/profiles/{profile}', headers=self.headers_for(2),
                                    json={'expires_at': future})
        self.assertEqual(updated.status_code, 200, updated.text)
        driver = FakeDriver()
        worker = IntentExecutor(self.db, driver)
        self.assertEqual(worker.queue_expired_profiles(), 0)
        while worker.run_one():
            pass
        self.assertEqual(driver.calls[-1][1]['action'], 'ensure')
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.db.connection.execute('UPDATE backend_profiles SET expires_at = ? WHERE id = ?', (past, profile))
        self.assertEqual(worker.queue_expired_profiles(), 1)
        self.assertEqual(worker.queue_expired_profiles(), 0)
        self.assertTrue(worker.run_one())
        self.assertEqual(driver.calls[-1][1]['action'], 'delete')
        self.assertEqual(driver.calls[-1][1]['desired_revision'], 4)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_grants WHERE profile_id = ?', (profile,)).fetchone()[0], 1)
        extended = self.client.patch(f'/api/v1/profiles/{profile}', headers=self.headers_for(4),
                                    json={'expires_at': None})
        self.assertEqual(extended.status_code, 200, extended.text)
        self.assertTrue(worker.run_one())
        self.assertEqual(driver.calls[-1][1]['action'], 'ensure')
        self.assertEqual(worker.queue_expired_profiles(), 0)

    def test_expiry_without_targets_or_already_revoked_revision_does_not_loop(self):
        profile = self.prepare()
        self.db.connection.execute('UPDATE backend_profiles SET owner_account_id = NULL WHERE id = ?', (profile,))
        worker = IntentExecutor(self.db, FakeDriver())
        self.client.patch(f'/api/v1/profiles/{profile}', headers=self.headers_for(1),
                          json={'expires_at': '2000-01-01T00:00:00Z'})
        self.assertEqual(worker.queue_expired_profiles(), 0)
        self.grants(profile, 2, [{'node_key': 'node', 'protocol': 'awg'}])
        self.assertEqual(worker.queue_expired_profiles(), 0)
        self.assertTrue(worker.run_one())
        self.assertEqual(worker.driver.calls[-1][1]['action'], 'delete')
        self.assertEqual(worker.queue_expired_profiles(), 0)
