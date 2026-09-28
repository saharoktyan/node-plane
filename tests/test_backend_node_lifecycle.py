import unittest

from backend.authorization import Actor, Principal, PrincipalKind
from backend.executor import IntentExecutor
from backend.node_lifecycle import NodeLifecycle
from tests.test_backend_executor import FakeDriver
from tests import test_backend_operations


class BackendNodeLifecycleTests(unittest.TestCase):
    setUp = test_backend_operations.BackendOperationTests.setUp
    headers_for = test_backend_operations.BackendOperationTests.headers_for
    create = test_backend_operations.BackendOperationTests.create
    prepare = test_backend_operations.BackendOperationTests.prepare
    grants = test_backend_operations.BackendOperationTests.grants

    def actor(self):
        return Actor(Principal('local-drain', PrincipalKind.ACCOUNT,
            frozenset({'maintenance.manage'}), self.admin.id), self.admin)

    def test_drain_supersedes_queued_ensure_and_revokes_historical_targets(self):
        profile = self.prepare()
        self.grants(profile, 1, [{'node_key': 'node', 'protocol': 'awg'}])
        self.grants(profile, 2, [])
        self.grants(profile, 3, [{'node_key': 'node', 'protocol': 'awg'}])
        lifecycle = NodeLifecycle(self.db)
        result = lifecycle.start_drain(self.actor(), 'node')
        self.assertEqual(result['status'], 'draining')
        self.assertEqual(lifecycle.start_drain(self.actor(), 'node'), result)
        self.assertFalse(lifecycle.drain_status(self.actor(), 'node')['revocations_complete'])
        self.assertEqual(self.db.connection.execute('SELECT enabled FROM backend_nodes WHERE key = ?', ('node',)).fetchone()[0], 0)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_grants WHERE node_key = ?', ('node',)).fetchone()[0], 0)
        self.assertEqual(self.db.connection.execute('SELECT desired_revision FROM backend_profiles WHERE id = ?', (profile,)).fetchone()[0], 5)
        driver = FakeDriver()
        executor = IntentExecutor(self.db, driver)
        while executor.run_one():
            pass
        self.assertEqual([intent['action'] for _, intent in driver.calls], ['delete', 'delete'])
        self.assertEqual(driver.calls[-1][1]['desired_revision'], 5)
        self.assertEqual(self.db.connection.execute('SELECT status FROM backend_operations WHERE id = ?',
            (result['operation_ids'][0],)).fetchone()[0], 'succeeded')
        self.assertTrue(lifecycle.drain_status(self.actor(), 'node')['revocations_complete'])
        self.assertEqual(self.grants(profile, 5, [{'node_key': 'node', 'protocol': 'awg'}]).status_code, 422)

    def test_drain_without_profiles_is_ready_for_later_runtime_cleanup(self):
        self.prepare()
        lifecycle = NodeLifecycle(self.db)
        self.assertEqual(lifecycle.start_drain(self.actor(), 'node')['operation_ids'], [])
        self.assertTrue(lifecycle.drain_status(self.actor(), 'node')['revocations_complete'])

    def test_blocked_delete_does_not_claim_revocation_complete(self):
        profile = self.prepare()
        self.grants(profile, 1, [{'node_key': 'node', 'protocol': 'awg'}])
        lifecycle = NodeLifecycle(self.db)
        lifecycle.start_drain(self.actor(), 'node')
        worker = IntentExecutor(self.db, FakeDriver(failure=True))
        while worker.run_one():
            pass
        status = lifecycle.drain_status(self.actor(), 'node')
        self.assertFalse(status['revocations_complete'])
        self.assertEqual(status['blocked_tasks'], 1)

    def test_drain_retries_revocation_for_target_without_current_grant(self):
        profile = self.prepare()
        self.grants(profile, 1, [{'node_key': 'node', 'protocol': 'xray'}])
        self.grants(profile, 2, [])
        driver = FakeDriver()
        worker = IntentExecutor(self.db, driver)
        while worker.run_one():
            pass
        result = NodeLifecycle(self.db).start_drain(self.actor(), 'node')
        self.assertEqual(len(result['operation_ids']), 1)
        self.assertEqual(self.db.connection.execute('SELECT desired_revision FROM backend_profiles WHERE id = ?', (profile,)).fetchone()[0], 4)
        self.assertTrue(worker.run_one())
        self.assertEqual(driver.calls[-1][1]['action'], 'delete')
        self.assertEqual(driver.calls[-1][1]['desired_revision'], 4)

    def test_drain_rolls_back_when_outbox_fails(self):
        profile = self.prepare()
        self.grants(profile, 1, [{'node_key': 'node', 'protocol': 'awg'}])
        with self.db.transaction() as conn:
            conn.execute('''CREATE TRIGGER fail_drain BEFORE INSERT ON backend_node_drains
                BEGIN SELECT RAISE(ABORT, 'temporary persistence fault'); END''')
        with self.assertRaises(Exception):
            NodeLifecycle(self.db).start_drain(self.actor(), 'node')
        self.assertEqual(self.db.connection.execute('SELECT enabled FROM backend_nodes WHERE key = ?', ('node',)).fetchone()[0], 1)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_grants WHERE node_key = ?', ('node',)).fetchone()[0], 1)
        self.assertEqual(self.db.connection.execute('SELECT desired_revision FROM backend_profiles WHERE id = ?', (profile,)).fetchone()[0], 2)

    def test_drain_rejects_non_admin(self):
        self.prepare()
        account = self.identities.resolve_telegram(102)
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_accounts SET status = 'approved' WHERE id = ?", (account.id,))
        member = self.identities.get_account(account.id)
        actor = Actor(Principal('local-drain', PrincipalKind.ACCOUNT,
            frozenset({'maintenance.manage'}), member.id), member)
        with self.assertRaises(Exception):
            NodeLifecycle(self.db).start_drain(actor, 'node')
        self.assertEqual(self.db.connection.execute('SELECT enabled FROM backend_nodes WHERE key = ?', ('node',)).fetchone()[0], 1)

    def test_cleanup_requires_confirmed_revocation_and_advances_one_phase(self):
        profile = self.prepare()
        self.grants(profile, 1, [{'node_key': 'node', 'protocol': 'awg'}])
        lifecycle = NodeLifecycle(self.db)
        lifecycle.start_drain(self.actor(), 'node')
        calls = []
        class Driver:
            def decommission(self, node_key, command_id, phase):
                calls.append((node_key, command_id, phase))
                return 'OK'
        driver = Driver()
        with self.assertRaises(Exception):
            lifecycle.cleanup(self.actor(), 'node', driver)
        self.assertEqual(calls, [])
        worker = IntentExecutor(self.db, FakeDriver())
        while worker.run_one():
            pass
        phases = [lifecycle.cleanup(self.actor(), 'node', driver)['phase'] for _ in range(3)]
        self.assertEqual(phases, ['prepared', 'runtime_deleted', 'uninstall_scheduled'])
        self.assertEqual([call[2] for call in calls], ['prepare', 'delete_runtime', 'uninstall'])
        self.assertEqual(len({call[1] for call in calls}), 1)
        self.assertEqual(lifecycle.cleanup(self.actor(), 'node', driver)['phase'], 'uninstall_scheduled')
        self.assertEqual(len(calls), 3)
        self.assertEqual(lifecycle.drain_status(self.actor(), 'node')['cleanup_phase'], 'uninstall_scheduled')
        self.assertIsNotNone(self.db.connection.execute('SELECT key FROM backend_nodes WHERE key = ?', ('node',)).fetchone())

    def test_cleanup_failure_retries_safe_phases_but_not_ambiguous_uninstall(self):
        self.prepare()
        lifecycle = NodeLifecycle(self.db)
        lifecycle.start_drain(self.actor(), 'node')
        calls = []
        class Driver:
            fail_phase = 'prepare'
            def decommission(self, node_key, command_id, phase):
                calls.append((command_id, phase))
                if phase == self.fail_phase:
                    raise TimeoutError('unknown remote result')
                return 'OK'
        driver = Driver()
        with self.assertRaises(TimeoutError):
            lifecycle.cleanup(self.actor(), 'node', driver)
        self.assertEqual(lifecycle.drain_status(self.actor(), 'node')['cleanup_phase'], 'preparing')
        driver.fail_phase = None
        lifecycle.cleanup(self.actor(), 'node', driver)
        driver.fail_phase = 'delete_runtime'
        with self.assertRaises(TimeoutError):
            lifecycle.cleanup(self.actor(), 'node', driver)
        self.assertEqual(lifecycle.drain_status(self.actor(), 'node')['cleanup_phase'], 'prepared')
        driver.fail_phase = None
        lifecycle.cleanup(self.actor(), 'node', driver)
        driver.fail_phase = 'uninstall'
        with self.assertRaises(TimeoutError):
            lifecycle.cleanup(self.actor(), 'node', driver)
        self.assertEqual(lifecycle.cleanup(self.actor(), 'node', driver)['phase'], 'uninstall_uncertain')
        self.assertEqual([phase for _, phase in calls],
            ['prepare', 'prepare', 'delete_runtime', 'delete_runtime', 'uninstall'])
        self.assertEqual(len({command for command, _ in calls}), 1)

    def test_profile_edits_after_cleanup_starts_do_not_address_fenced_node(self):
        profile = self.prepare()
        self.grants(profile, 1, [{'node_key': 'node', 'protocol': 'xray'}])
        lifecycle = NodeLifecycle(self.db)
        lifecycle.start_drain(self.actor(), 'node')
        worker = IntentExecutor(self.db, FakeDriver())
        while worker.run_one():
            pass
        lifecycle.cleanup(self.actor(), 'node', type('Driver', (), {
            'decommission': lambda self, *args: 'OK'})())
        edited = self.client.patch(f'/api/v1/profiles/{profile}', headers=self.headers_for(3),
                                   json={'display_name': 'Changed'})
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual(edited.json()['runtime_status'], 'no_targets')
        operation_id = edited.json()['operation_id']
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_operation_tasks WHERE operation_id = ?',
            (operation_id,)).fetchone()[0], 0)

    def test_registry_only_removal_retains_uncertainty_and_never_dispatches_again(self):
        profile = self.prepare()
        self.grants(profile, 1, [{'node_key': 'node', 'protocol': 'awg'}])
        lifecycle = NodeLifecycle(self.db)
        reason = 'VPS subscription expired and agent is unreachable'
        result = lifecycle.retire_registry_only(self.actor(), 'node', reason)
        self.assertEqual(result['mode'], 'registry_only')
        self.assertGreaterEqual(result['unfinished_tasks'], 1)
        self.assertEqual(lifecycle.retire_registry_only(self.actor(), 'node', reason), result)
        with self.assertRaises(Exception):
            lifecycle.retire_registry_only(self.actor(), 'node', 'Different reason for removal')
        row = self.db.connection.execute('SELECT mode, reason, unfinished_tasks FROM backend_node_retirements WHERE node_key = ?', ('node',)).fetchone()
        self.assertEqual(row['reason'], reason)
        self.assertIsNone(self.db.connection.execute('SELECT key FROM backend_nodes WHERE key = ?', ('node',)).fetchone())
        self.assertIsNone(self.db.connection.execute('SELECT profile_id FROM backend_grants WHERE node_key = ?', ('node',)).fetchone())
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM backend_operation_tasks WHERE node_key = ? AND status IN ('awaiting_executor', 'running', 'blocked')", ('node',)).fetchone()[0], 0)
        self.assertFalse(IntentExecutor(self.db, FakeDriver()).run_one())
        edited = self.client.patch(f'/api/v1/profiles/{profile}', headers=self.headers_for(3), json={'display_name': 'Still here'})
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual(edited.json()['runtime_status'], 'no_targets')

    def test_registry_only_requires_reason_and_admin(self):
        self.prepare()
        lifecycle = NodeLifecycle(self.db)
        for reason in ('', 'too short', 'x' * 501):
            with self.assertRaises(Exception):
                lifecycle.retire_registry_only(self.actor(), 'node', reason)
        account = self.identities.resolve_telegram(102)
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_accounts SET status = 'approved' WHERE id = ?", (account.id,))
        member = self.identities.get_account(account.id)
        actor = Actor(Principal('local-registry-removal', PrincipalKind.ACCOUNT,
            frozenset({'maintenance.manage'}), member.id), member)
        with self.assertRaises(Exception):
            lifecycle.retire_registry_only(actor, 'node', 'Agent unavailable after VPS expiration')
        self.assertIsNotNone(self.db.connection.execute('SELECT key FROM backend_nodes WHERE key = ?', ('node',)).fetchone())

    def test_verified_retirement_requires_uninstall_and_host_evidence(self):
        self.prepare()
        lifecycle = NodeLifecycle(self.db)
        class Verifier:
            calls = 0
            fail = False
            local = False
            ssh_target = 'root@node.example'
            fingerprint = 'a' * 64
            def capture_identity(self, node_key):
                return self.fingerprint
            def verify(self, node_key, expected_fingerprint):
                self.calls += 1
                if self.fail:
                    raise TimeoutError('host unreachable')
                return {'method': 'ssh', 'target': 'root@node.example',
                    'host_fingerprint': expected_fingerprint,
                    'checked_at': '2026-09-28T00:00:00+00:00',
                    'result': 'agent_and_standard_artifacts_absent'}
        verifier = Verifier()
        self.assertEqual(lifecycle.bind_verification_target(self.actor(), 'node', verifier)['target'], 'root@node.example')
        self.assertEqual(lifecycle.bind_verification_target(self.actor(), 'node', verifier)['host_fingerprint'], 'a' * 64)
        verifier.fingerprint = 'b' * 64
        with self.assertRaises(Exception):
            lifecycle.bind_verification_target(self.actor(), 'node', verifier)
        verifier.fingerprint = 'a' * 64
        verifier.ssh_target = 'root@wrong.example'
        with self.assertRaises(Exception):
            lifecycle.bind_verification_target(self.actor(), 'node', verifier)
        verifier.ssh_target = 'root@node.example'
        lifecycle.start_drain(self.actor(), 'node')
        with self.assertRaises(Exception):
            lifecycle.retire_verified(self.actor(), 'node', verifier)
        self.assertEqual(verifier.calls, 0)
        driver = type('Driver', (), {'decommission': lambda self, *args: 'OK'})()
        for _ in range(3):
            lifecycle.cleanup(self.actor(), 'node', driver)
        verifier.fail = True
        with self.assertRaises(TimeoutError):
            lifecycle.retire_verified(self.actor(), 'node', verifier)
        self.assertIsNotNone(self.db.connection.execute('SELECT key FROM backend_nodes WHERE key = ?', ('node',)).fetchone())
        verifier.fail = False
        result = lifecycle.retire_verified(self.actor(), 'node', verifier)
        self.assertEqual(result['mode'], 'verified')
        self.assertEqual(lifecycle.retire_verified(self.actor(), 'node', verifier), result)
        self.assertEqual(verifier.calls, 2)
        row = self.db.connection.execute('SELECT mode, evidence_json FROM backend_node_retirements WHERE node_key = ?', ('node',)).fetchone()
        self.assertEqual(row['mode'], 'verified')
        self.assertIn('root@node.example', row['evidence_json'])
        self.assertIsNone(self.db.connection.execute('SELECT key FROM backend_nodes WHERE key = ?', ('node',)).fetchone())


if __name__ == '__main__':
    unittest.main()
