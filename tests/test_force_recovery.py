from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

import grpc

from backend.authorization import AccessDenied, Actor, Principal, PrincipalKind, ADMIN_PERMISSIONS
from backend.recovery import act, overview, history
from backend.node_settings import NodeSettingsService
from backend.node_operations import NodeOperations
from backend.node_bootstrap import NodeBootstrapService
from backend.executor import IntentExecutor
from backend.profiles import ProfileRepository
from backend.devices import DeviceRepository
from backend.operations import OperationRepository
from backend.force_recovery import cancel, spec_for

from tests import test_backend_node_jobs as fixture_node_jobs
from tests import test_backend_updates as fixture_updates
from tests import test_backend_executor as fixture_executor


class FakeRpcError(grpc.RpcError):
    def __init__(self, code, details=''):
        super().__init__()
        self._code = code
        self._details = details

    def code(self):
        return self._code

    def details(self):
        return self._details


class FakeDriver:
    def __init__(self, *, prepare_receipt=None, retire_receipt=None, prepare_error=None, retire_errors=None, on_retire=None):
        self.calls = []
        self.prepare_receipt = prepare_receipt
        self.retire_receipt = retire_receipt
        self.prepare_error = prepare_error
        self.retire_errors = list(retire_errors) if retire_errors is not None else []
        self.on_retire = on_retire

    def force_recovery(self, spec, phase):
        self.calls.append((phase, spec))
        if phase == 'prepare':
            if self.prepare_error:
                raise self.prepare_error
            if self.prepare_receipt is not None:
                return self.prepare_receipt
            return {'ticket_id': spec['ticket_id'], 'node_key': spec['node_key'], 'restart_required': True}
        if phase == 'retire':
            if self.on_retire:
                self.on_retire(spec)
            if self.retire_errors:
                err = self.retire_errors.pop(0)
                raise err
            if self.retire_receipt is not None:
                return self.retire_receipt
            return {
                'ticket_id': spec['ticket_id'],
                'node_key': spec['node_key'],
                'command_id': spec['command_id'],
                'kind': spec['kind'],
                'forced': True,
                'outcome_unknown': True,
            }
        raise ValueError(f'unknown phase {phase}')

    def inspect_node(self, node_key):
        return {'health_state': 'running', 'xray_config_present': True, 'awg_config_present': True}

    def apply_node_settings(self, command_id, intent):
        from backend.node_settings import _digest
        self.calls.append(('apply_node_settings', command_id, intent))
        return json.dumps({
            'node_key': intent['node_key'],
            'revision': intent['revision'],
            'settings_sha256': _digest(intent)
        })

    def execute(self, task_id, intent):
        self.calls.append(('execute', task_id, intent))
        return {
            'driver_operation_id': str(task_id),
            'succeeded': True,
            'result_json': json.dumps({'status': 'ok'})
        }


class ForceRecoveryTests(TestCase):
    setUp = fixture_node_jobs.BackendNodeJobTests.setUp
    queue = fixture_node_jobs.BackendNodeJobTests.queue

    def confirmation(self, operation_id):
        return {
            'confirm_operation_id': operation_id,
            'acknowledge_unknown_outcome': True,
        }

    def test_settings_force_recovery_success(self):
        self.db.connection.execute("UPDATE backend_nodes SET applied_revision=0 WHERE key='n1'")
        task = NodeSettingsService(self.db).queue(self.actor, 'n1', revision=1, command_key=str(uuid4()))
        self.db.connection.execute("UPDATE backend_node_settings_tasks SET status='blocked'")

        driver = FakeDriver()
        result = act(self.db, self.actor, driver, 'settings', task['id'], 'force',
                     confirmation=self.confirmation(task['id']))

        self.assertEqual(result['status'], 'superseded')
        self.assertEqual(result['outcome'], 'force_cancelled')
        self.assertEqual(result['observation'], {'forced': True, 'outcome_unknown': True})
        self.assertIsNone(result['replacement_id'])

        row = self.db.connection.execute("SELECT * FROM backend_node_settings_tasks WHERE id=?", (task['id'],)).fetchone()
        self.assertEqual(row['status'], 'superseded')
        res = json.loads(row['result_json'])
        self.assertEqual(res['error_code'], 'operator_force_cancelled')
        self.assertTrue(res['forced'])
        self.assertTrue(res['outcome_unknown'])

        node = self.db.connection.execute("SELECT desired_revision FROM backend_nodes WHERE key='n1'").fetchone()
        self.assertEqual(node['desired_revision'], 2)

        audit_entry = history(self.db, self.actor)['items'][0]
        self.assertEqual(audit_entry['action'], 'force')
        self.assertEqual(audit_entry['outcome'], 'force_cancelled')
        self.assertEqual(audit_entry['status'], 'superseded')

        self.assertEqual([call[0] for call in driver.calls], ['prepare', 'retire'])

    def test_node_job_force_recovery_success(self):
        job = self.queue('sync_runtime')
        self.db.connection.execute("UPDATE backend_node_jobs SET status='blocked'")

        driver = FakeDriver()
        result = act(self.db, self.actor, driver, 'node', job['id'], 'force',
                     confirmation=self.confirmation(job['id']))

        self.assertEqual(result['status'], 'superseded')
        self.assertEqual(result['outcome'], 'force_cancelled')

        row = self.db.connection.execute("SELECT * FROM backend_node_jobs WHERE id=?", (job['id'],)).fetchone()
        self.assertEqual(row['status'], 'superseded')
        res = json.loads(row['result_json'])
        self.assertEqual(res['error_code'], 'operator_force_cancelled')
        self.assertTrue(res['forced'])

        node = self.db.connection.execute("SELECT desired_revision FROM backend_nodes WHERE key='n1'").fetchone()
        self.assertEqual(node['desired_revision'], 2)

    def test_profile_force_recovery_success_and_scoped_requeue(self):
        from backend.nodes import NodeService
        NodeService(self.db).command(self.actor, action='create', command_key=str(uuid4()), values={
            'key': 'n2', 'title': 'Node 2', 'region': 'US', 'transport': 'ssh', 'ssh_target': 'root@n2.example',
            'protocols': ['awg'], 'xray_transports': [], 'settings': self.settings})
        self.db.connection.execute("UPDATE backend_nodes SET enabled=1, applied_revision=1 WHERE key='n2'")

        repo = ProfileRepository(self.db)
        profile_id = repo.create_profile(runtime_name='user-test', display_name='User Test', owner_account_id=self.actor.account.id)
        profile_row = self.db.connection.execute("SELECT * FROM backend_profiles WHERE id=?", (profile_id,)).fetchone()
        DeviceRepository.ensure_default(self.db.connection, profile_row)
        self.db.connection.execute("INSERT INTO backend_grants(profile_id, node_key, protocol) VALUES (?, 'n1', 'awg'), (?, 'n2', 'awg')", (profile_id, profile_id))

        targets = OperationRepository.targets(self.db.connection, profile_id)
        op = OperationRepository.record(self.db.connection, self.actor, profile_id, targets)

        t1 = self.db.connection.execute("SELECT id FROM backend_operation_tasks WHERE operation_id=? AND node_key='n1'", (op['id'],)).fetchone()['id']
        t2 = self.db.connection.execute("SELECT id FROM backend_operation_tasks WHERE operation_id=? AND node_key='n2'", (op['id'],)).fetchone()['id']
        self.db.connection.execute("UPDATE backend_operation_tasks SET status='blocked' WHERE id=?", (t1,))

        old_op = OperationRepository.record(self.db.connection, self.actor, profile_id, targets)
        old_t1 = self.db.connection.execute("SELECT id FROM backend_operation_tasks WHERE operation_id=? AND node_key='n1'", (old_op['id'],)).fetchone()['id']
        old_t2 = self.db.connection.execute("SELECT id FROM backend_operation_tasks WHERE operation_id=? AND node_key='n2'", (old_op['id'],)).fetchone()['id']

        driver = FakeDriver()
        result = act(self.db, self.actor, driver, 'profile', t1, 'force',
                     confirmation=self.confirmation(t1))

        self.assertEqual(result['status'], 'superseded')
        self.assertEqual(result['outcome'], 'force_cancelled')

        row_t1 = self.db.connection.execute("SELECT status, result_json FROM backend_operation_tasks WHERE id=?", (t1,)).fetchone()
        self.assertEqual(row_t1['status'], 'superseded')
        # Forced cancellation records an unverified outcome; old marker does not count as verified revoke
        res_t1 = json.loads(row_t1['result_json'])
        self.assertEqual(res_t1['error_code'], 'operator_force_cancelled')
        self.assertTrue(res_t1['forced'])
        self.assertTrue(res_t1['outcome_unknown'])

        row_old_t1 = self.db.connection.execute("SELECT status FROM backend_operation_tasks WHERE id=?", (old_t1,)).fetchone()
        self.assertEqual(row_old_t1['status'], 'superseded')

        row_t2 = self.db.connection.execute("SELECT status FROM backend_operation_tasks WHERE id=?", (t2,)).fetchone()
        self.assertEqual(row_t2['status'], 'awaiting_executor')

        # CANCEL ONLY: NO automatic requeue during force cancellation
        fresh_tasks = self.db.connection.execute(
            "SELECT node_key, protocol, status FROM backend_operation_tasks WHERE operation_id NOT IN (?, ?) ORDER BY id",
            (op['id'], old_op['id'])
        ).fetchall()
        self.assertEqual(len(fresh_tasks), 0)

        # Fresh scoped sync via backend.force_recovery.resume_forced_profile_targets
        from backend.force_recovery import forced_profile_targets, resume_forced_profile_targets
        forced = forced_profile_targets(self.db.connection, 'n1')
        self.assertEqual(forced, {profile_id: {('n1', 'awg')}})
        self.assertEqual(forced_profile_targets(self.db.connection, 'n2'), {})

        resume_forced_profile_targets(self.db.connection, self.actor, 'n1')
        resumed_tasks = self.db.connection.execute(
            "SELECT node_key, protocol, status FROM backend_operation_tasks WHERE operation_id NOT IN (?, ?) ORDER BY id",
            (op['id'], old_op['id'])
        ).fetchall()
        self.assertEqual(len(resumed_tasks), 1)
        self.assertEqual((resumed_tasks[0]['node_key'], resumed_tasks[0]['protocol']), ('n1', 'awg'))
        self.assertEqual(resumed_tasks[0]['status'], 'awaiting_executor')
        self.assertEqual(forced_profile_targets(self.db.connection, 'n1'), {})

    def test_profile_force_recovery_resumed_on_settings_apply_and_reconcile(self):
        repo = ProfileRepository(self.db)
        member = self.db.connection.execute("INSERT INTO backend_accounts(role, status) VALUES ('member', 'approved') RETURNING id").fetchone()
        profile_id = repo.create_profile(runtime_name='user-sync', display_name='User Sync', owner_account_id=member['id'])
        profile_row = self.db.connection.execute("SELECT * FROM backend_profiles WHERE id=?", (profile_id,)).fetchone()
        DeviceRepository.ensure_default(self.db.connection, profile_row)
        self.db.connection.execute("INSERT INTO backend_grants(profile_id, node_key, protocol) VALUES (?, 'n1', 'awg')", (profile_id,))

        targets = OperationRepository.targets(self.db.connection, profile_id)
        op = OperationRepository.record(self.db.connection, self.actor, profile_id, targets)
        t1 = self.db.connection.execute("SELECT id FROM backend_operation_tasks WHERE operation_id=? AND node_key='n1'", (op['id'],)).fetchone()['id']
        self.db.connection.execute("UPDATE backend_operation_tasks SET status='blocked' WHERE id=?", (t1,))

        driver = FakeDriver()
        act(self.db, self.actor, driver, 'profile', t1, 'force', confirmation=self.confirmation(t1))

        # 1. Resumed on next explicit settings apply (NodeSettingsExecutor._finish)
        self.db.connection.execute("UPDATE backend_nodes SET desired_revision=2, applied_revision=1 WHERE key='n1'")
        task = NodeSettingsService(self.db).queue(self.actor, 'n1', revision=2, command_key=str(uuid4()))
        task_row = self.db.connection.execute("SELECT * FROM backend_node_settings_tasks WHERE id=?", (task['id'],)).fetchone()
        from backend.node_settings import NodeSettingsExecutor, _digest
        task_intent = json.loads(task_row['intent_json'])
        result_payload = json.dumps({'node_key': 'n1', 'revision': 2, 'settings_sha256': _digest(task_intent)})
        with self.db.transaction() as conn:
            NodeSettingsExecutor._finish(conn, task_row, result_payload)

        resumed = self.db.connection.execute(
            "SELECT node_key, protocol, status FROM backend_operation_tasks WHERE operation_id != ? ORDER BY id",
            (op['id'],)
        ).fetchall()
        self.assertEqual(len(resumed), 1)
        self.assertEqual((resumed[0]['node_key'], resumed[0]['protocol']), ('n1', 'awg'))

        # 2. Resumed on next explicit Reconcile access (NodeOperations.complete for reconcile_access)
        # Block and force cancel this newly resumed task
        new_task_id = self.db.connection.execute(
            "SELECT id FROM backend_operation_tasks WHERE operation_id != ? ORDER BY id DESC LIMIT 1",
            (op['id'],)
        ).fetchone()['id']
        self.db.connection.execute("UPDATE backend_operation_tasks SET status='blocked' WHERE id=?", (new_task_id,))
        act(self.db, self.actor, driver, 'profile', new_task_id, 'force', confirmation=self.confirmation(new_task_id))

        reconcile_job = NodeOperations(self.db).queue(self.actor, 'n1', 'reconcile_access', revision=2, command_key=str(uuid4()))
        self.assertTrue(NodeOperations(self.db).run_one())

        reconciled_tasks = self.db.connection.execute(
            "SELECT node_key, protocol, status FROM backend_operation_tasks WHERE id NOT IN (?, ?) ORDER BY id",
            (t1, new_task_id)
        ).fetchall()
        self.assertEqual(len(reconciled_tasks), 1)
        self.assertEqual((reconciled_tasks[0]['node_key'], reconciled_tasks[0]['protocol']), ('n1', 'awg'))

    def test_profile_force_recovery_supersedes_dependent_settings_and_explicit_apply_requeues(self):
        from backend.grant_policies import create_schema as create_grant_schema
        from backend.temporary_configs import TemporaryConfigService
        from backend.nodes import NodeService
        from backend.force_recovery import forced_profile_targets
        from backend.node_settings import NodeSettingsExecutor

        with self.db.transaction() as conn:
            create_grant_schema(conn)
        TemporaryConfigService(self.db).initialize_schema()

        # 1. Initialized applied protocols receipt for n1
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO backend_node_settings_tasks(id, node_key, actor_account_id, command_key, revision, intent_json, status, result_json) "
                "VALUES (?, 'n1', ?, ?, 1, ?, 'succeeded', '{}')",
                (str(uuid4()), self.actor.account.id, str(uuid4()), json.dumps({
                    'node_key': 'n1',
                    'revision': 1,
                    'protocols': ['awg', 'xray'],
                    'settings': self.settings
                }))
            )

        # Create member profile with device and grants for n1 on awg and xray
        repo = ProfileRepository(self.db)
        member = self.db.connection.execute(
            "INSERT INTO backend_accounts(role, status) VALUES ('member', 'approved') RETURNING id"
        ).fetchone()
        profile_id = repo.create_profile(runtime_name='user-reg', display_name='User Reg', owner_account_id=member['id'])
        profile_row = self.db.connection.execute("SELECT * FROM backend_profiles WHERE id=?", (profile_id,)).fetchone()
        DeviceRepository.ensure_default(self.db.connection, profile_row)
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO backend_grants(profile_id, node_key, protocol) VALUES (?, 'n1', 'awg'), (?, 'n1', 'xray')",
                (profile_id, profile_id)
            )

        # Unrelated protocol task on n1 for retained protocol awg (already provisioned)
        op = OperationRepository.record(self.db.connection, self.actor, profile_id, {('n1', 'awg')})
        awg_task = self.db.connection.execute(
            "SELECT * FROM backend_operation_tasks WHERE operation_id=? AND protocol='awg'", (op['id'],)
        ).fetchone()
        self.assertIsNotNone(awg_task)
        self.db.connection.execute("UPDATE backend_operation_tasks SET status='succeeded' WHERE id=?", (awg_task['id'],))

        # Unrelated historical node job on n1
        hist_job_id = str(uuid4())
        self.db.connection.execute(
            "INSERT INTO backend_node_jobs(id, node_key, actor_id, command_key, action, revision, intent_json, status, result_json) "
            "VALUES (?, 'n1', ?, ?, 'sync_runtime', 1, '{}', 'succeeded', '{\"verified\": true}')",
            (hist_job_id, self.actor.account.id, str(uuid4()))
        )

        # Unrelated node n2 with its own unclaimed settings task to verify it is NOT superseded
        NodeService(self.db).command(self.actor, action='create', command_key=str(uuid4()), values={
            'key': 'n2', 'title': 'Node 2', 'region': 'US', 'transport': 'ssh', 'ssh_target': 'root@n2.example',
            'protocols': ['awg'], 'xray_transports': [], 'settings': self.settings
        })
        self.db.connection.execute("UPDATE backend_nodes SET enabled=1, applied_revision=0 WHERE key='n2'")
        unrelated_n2_task = NodeSettingsService(self.db).queue(self.actor, 'n2', revision=1, command_key=str(uuid4()))
        self.assertEqual(unrelated_n2_task['status'], 'awaiting_executor')

        # 2. Change desired protocols on n1 to remove xray (Save gap: desired=2, applied=1)
        NodeService(self.db).command(
            self.actor, action='edit', node_key='n1', revision=1,
            command_key=str(uuid4()), values={'protocols': ['awg']}
        )

        # Deletion task for xray is created and waiting
        delete_task = self.db.connection.execute(
            "SELECT * FROM backend_operation_tasks WHERE node_key='n1' AND protocol='xray' AND action='delete' AND status='awaiting_executor'"
        ).fetchone()
        self.assertIsNotNone(delete_task)

        # 3. Queue settings apply waiting for revision 2 on n1
        settings_task = NodeSettingsService(self.db).queue(self.actor, 'n1', revision=2, command_key=str(uuid4()))
        self.assertEqual(settings_task['status'], 'awaiting_executor')
        queued_intent = json.loads(self.db.connection.execute(
            "SELECT intent_json FROM backend_node_settings_tasks WHERE id=?", (settings_task['id'],)
        ).fetchone()['intent_json'])
        self.assertEqual(queued_intent['protocol_plan']['remove_protocols'], ['xray'])

        # 4. Block deletion task
        self.db.connection.execute("UPDATE backend_operation_tasks SET status='blocked' WHERE id=?", (delete_task['id'],))
        tasks_before_cancel = {r['id'] for r in self.db.connection.execute("SELECT id FROM backend_operation_tasks").fetchall()}

        # 5. Force cancel deletion task via FakeDriver
        driver = FakeDriver()
        result = act(self.db, self.actor, driver, 'profile', delete_task['id'], 'force',
                     confirmation=self.confirmation(delete_task['id']))
        self.assertEqual(result['status'], 'superseded')
        self.assertEqual(result['outcome'], 'force_cancelled')
        self.assertEqual(result['observation'], {'forced': True, 'outcome_unknown': True})

        # 6. Verify only dependent unclaimed settings task superseded:
        dep_settings_row = self.db.connection.execute(
            "SELECT status, result_json FROM backend_node_settings_tasks WHERE id=?", (settings_task['id'],)
        ).fetchone()
        self.assertEqual(dep_settings_row['status'], 'superseded')
        dep_res = json.loads(dep_settings_row['result_json'])
        self.assertEqual(dep_res['error_code'], 'operator_force_cancelled_dependency')
        self.assertEqual(dep_res['cancelled_task_id'], delete_task['id'])

        # The cancelled profile deletion task is superseded with unverified outcome
        del_row = self.db.connection.execute(
            "SELECT status, result_json FROM backend_operation_tasks WHERE id=?", (delete_task['id'],)
        ).fetchone()
        self.assertEqual(del_row['status'], 'superseded')
        del_res = json.loads(del_row['result_json'])
        self.assertEqual(del_res['error_code'], 'operator_force_cancelled')
        self.assertTrue(del_res['forced'])
        self.assertTrue(del_res['outcome_unknown'])

        # Unrelated node n2 settings task remains awaiting_executor
        unrelated_row = self.db.connection.execute(
            "SELECT status FROM backend_node_settings_tasks WHERE id=?", (unrelated_n2_task['id'],)
        ).fetchone()
        self.assertEqual(unrelated_row['status'], 'awaiting_executor')

        # Unrelated protocol task (awg on n1) remains unchanged (succeeded)
        awg_row = self.db.connection.execute(
            "SELECT status FROM backend_operation_tasks WHERE id=?", (awg_task['id'],)
        ).fetchone()
        self.assertEqual(awg_row['status'], 'succeeded')

        # Unrelated node job history remains unchanged (succeeded)
        job_row = self.db.connection.execute(
            "SELECT status FROM backend_node_jobs WHERE id=?", (hist_job_id,)
        ).fetchone()
        self.assertEqual(job_row['status'], 'succeeded')

        # History / audit for recovery recorded
        audit_entry = history(self.db, self.actor)['items'][0]
        self.assertEqual(audit_entry['action'], 'force')
        self.assertEqual(audit_entry['outcome'], 'force_cancelled')
        self.assertEqual(audit_entry['status'], 'superseded')
        self.assertEqual(audit_entry['operation_id'], delete_task['id'])

        # NO auto remote retry/new profile tasks immediately after force cancellation
        tasks_after_cancel = {r['id'] for r in self.db.connection.execute("SELECT id FROM backend_operation_tasks").fetchall()}
        self.assertEqual(tasks_after_cancel - tasks_before_cancel, set())
        self.assertEqual(forced_profile_targets(self.db.connection, 'n1'), {profile_id: {('n1', 'xray')}})

        # Drain unrelated n2 settings task so only n1 work is pending
        settings_worker = NodeSettingsExecutor(self.db, driver)
        self.assertTrue(settings_worker.run_one())
        self.assertEqual(self.db.connection.execute("SELECT status FROM backend_node_settings_tasks WHERE id=?", (unrelated_n2_task['id'],)).fetchone()['status'], 'succeeded')

        # 7. Next explicit NodeSettingsService.queue accepted and creates higher-profile-revision delete before cleanup
        profile_rev_before = self.db.connection.execute(
            "SELECT desired_revision FROM backend_profiles WHERE id=?", (profile_id,)
        ).fetchone()['desired_revision']

        next_settings_task = NodeSettingsService(self.db).queue(self.actor, 'n1', revision=2, command_key=str(uuid4()))
        self.assertEqual(next_settings_task['status'], 'awaiting_executor')

        profile_rev_after = self.db.connection.execute(
            "SELECT desired_revision FROM backend_profiles WHERE id=?", (profile_id,)
        ).fetchone()['desired_revision']
        self.assertGreater(profile_rev_after, profile_rev_before)

        # A higher-profile-revision delete task was created by revoke_removed_protocols
        new_del_task = self.db.connection.execute(
            "SELECT * FROM backend_operation_tasks WHERE node_key='n1' AND protocol='xray' AND action='delete' AND id != ?",
            (delete_task['id'],)
        ).fetchone()
        self.assertIsNotNone(new_del_task)
        self.assertEqual(new_del_task['status'], 'awaiting_executor')

        # 8. NodeSettingsExecutor cannot cleanup until confirmed revoke
        self.assertFalse(settings_worker.run_one())
        self.assertEqual([c for c in driver.calls if c[0] == 'apply_node_settings' and c[2]['node_key'] == 'n1'], [])
        task_still_awaiting = self.db.connection.execute(
            "SELECT status FROM backend_node_settings_tasks WHERE id=?", (next_settings_task['id'],)
        ).fetchone()
        self.assertEqual(task_still_awaiting['status'], 'awaiting_executor')

        # After IntentExecutor fake successful delete settings can finish
        intent_worker = IntentExecutor(self.db, driver)
        # Drain the delete task on n1
        delete_found = False
        while intent_worker.run_one():
            if self.db.connection.execute("SELECT status FROM backend_operation_tasks WHERE id=?", (new_del_task['id'],)).fetchone()['status'] == 'succeeded':
                delete_found = True
                break
        self.assertTrue(delete_found)

        # Now settings can finish
        self.assertTrue(settings_worker.run_one())
        task_finished = self.db.connection.execute(
            "SELECT status FROM backend_node_settings_tasks WHERE id=?", (next_settings_task['id'],)
        ).fetchone()
        self.assertEqual(task_finished['status'], 'succeeded')

        node_n1 = self.db.connection.execute(
            "SELECT applied_revision, desired_revision FROM backend_nodes WHERE key='n1'"
        ).fetchone()
        self.assertEqual((node_n1['desired_revision'], node_n1['applied_revision']), (2, 2))
        self.assertEqual(len([c for c in driver.calls if c[0] == 'apply_node_settings' and c[2]['node_key'] == 'n1']), 1)

    def test_bootstrap_force_recovery_with_node_child_success(self):
        node_job = self.queue('sync_runtime')
        self.db.connection.execute("UPDATE backend_node_jobs SET status='blocked'")

        bootstrap_id = str(uuid4())
        self.db.connection.execute(
            "INSERT INTO backend_node_bootstraps(id, node_key, actor_id, command_key, revision, phase, status, child_id, child_kind) VALUES (?, 'n1', ?, ?, 1, 'docker', 'blocked', ?, 'node-jobs')",
            (bootstrap_id, self.actor.account.id, str(uuid4()), node_job['id'])
        )

        driver = FakeDriver()
        result = act(self.db, self.actor, driver, 'bootstrap', bootstrap_id, 'force',
                     confirmation=self.confirmation(bootstrap_id))

        self.assertEqual(result['status'], 'superseded')
        self.assertEqual(result['outcome'], 'force_cancelled')

        child_row = self.db.connection.execute("SELECT status, result_json FROM backend_node_jobs WHERE id=?", (node_job['id'],)).fetchone()
        self.assertEqual(child_row['status'], 'superseded')
        self.assertEqual(json.loads(child_row['result_json'])['error_code'], 'operator_force_cancelled')

        b_row = self.db.connection.execute("SELECT status, error_code FROM backend_node_bootstraps WHERE id=?", (bootstrap_id,)).fetchone()
        self.assertEqual(b_row['status'], 'superseded')
        self.assertEqual(b_row['error_code'], 'operator_force_cancelled')

    def test_direct_node_child_cancellation_releases_bootstrap_parent(self):
        node_job = self.queue('sync_runtime')
        self.db.connection.execute("UPDATE backend_node_jobs SET status='blocked'")

        bootstrap_id = str(uuid4())
        self.db.connection.execute(
            "INSERT INTO backend_node_bootstraps(id, node_key, actor_id, command_key, revision, phase, status, child_id, child_kind) VALUES (?, 'n1', ?, ?, 1, 'docker', 'blocked', ?, 'node-jobs')",
            (bootstrap_id, self.actor.account.id, str(uuid4()), node_job['id'])
        )

        driver = FakeDriver()
        result = act(self.db, self.actor, driver, 'node', node_job['id'], 'force',
                     confirmation=self.confirmation(node_job['id']))

        self.assertEqual(result['status'], 'superseded')

        b_row = self.db.connection.execute("SELECT status, error_code FROM backend_node_bootstraps WHERE id=?", (bootstrap_id,)).fetchone()
        self.assertEqual(b_row['status'], 'superseded')
        self.assertEqual(b_row['error_code'], 'operator_force_cancelled')

    def test_bootstrap_with_non_node_child_rejected(self):
        rollout_id = str(uuid4())
        bootstrap_id = str(uuid4())
        self.db.connection.execute(
            "INSERT INTO backend_node_bootstraps(id, node_key, actor_id, command_key, revision, phase, status, child_id, child_kind) VALUES (?, 'n1', ?, ?, 1, 'agent', 'blocked', ?, 'agent-rollouts')",
            (bootstrap_id, self.actor.account.id, str(uuid4()), rollout_id)
        )
        driver = FakeDriver()
        with self.assertRaises(AccessDenied) as ctx:
            act(self.db, self.actor, driver, 'bootstrap', bootstrap_id, 'force',
                confirmation=self.confirmation(bootstrap_id))
        self.assertEqual(ctx.exception.code, 'force_operation_unsupported')
        self.assertEqual(ctx.exception.status, 409)

    def test_missing_or_mismatched_confirmation_rejected(self):
        job = self.queue('sync_runtime')
        self.db.connection.execute("UPDATE backend_node_jobs SET status='blocked'")
        driver = FakeDriver()

        # Missing confirmation completely
        with self.assertRaises(AccessDenied) as ctx:
            act(self.db, self.actor, driver, 'node', job['id'], 'force', confirmation=None)
        self.assertEqual(ctx.exception.code, 'force_confirmation_required')
        self.assertEqual(ctx.exception.status, 422)

        # Mismatched confirm_operation_id
        with self.assertRaises(AccessDenied) as ctx:
            act(self.db, self.actor, driver, 'node', job['id'], 'force',
                confirmation={'confirm_operation_id': str(uuid4()), 'acknowledge_unknown_outcome': True})
        self.assertEqual(ctx.exception.code, 'force_confirmation_required')

        # acknowledge_unknown_outcome is False
        with self.assertRaises(AccessDenied) as ctx:
            act(self.db, self.actor, driver, 'node', job['id'], 'force',
                confirmation={'confirm_operation_id': job['id'], 'acknowledge_unknown_outcome': False})
        self.assertEqual(ctx.exception.code, 'force_confirmation_required')

        # Invalid UUID
        with self.assertRaises(AccessDenied) as ctx:
            act(self.db, self.actor, driver, 'node', 'not-a-uuid', 'force',
                confirmation={'confirm_operation_id': 'not-a-uuid', 'acknowledge_unknown_outcome': True})
        self.assertEqual(ctx.exception.code, 'invalid_input')
        self.assertEqual(ctx.exception.status, 422)

    def test_permission_and_account_restrictions(self):
        job = self.queue('sync_runtime')
        self.db.connection.execute("UPDATE backend_node_jobs SET status='blocked'")
        driver = FakeDriver()

        # Missing nodes.execute
        actor_no_exec = Actor(Principal('admin', PrincipalKind.ACCOUNT, frozenset(ADMIN_PERMISSIONS - {'nodes.execute'}), self.actor.account.id), self.actor.account)
        with self.assertRaises(AccessDenied) as ctx:
            act(self.db, actor_no_exec, driver, 'node', job['id'], 'force', confirmation=self.confirmation(job['id']))
        self.assertEqual(ctx.exception.code, 'permission_denied')

        # Missing maintenance.manage
        actor_no_maint = Actor(Principal('admin', PrincipalKind.ACCOUNT, frozenset(ADMIN_PERMISSIONS - {'maintenance.manage'}), self.actor.account.id), self.actor.account)
        with self.assertRaises(AccessDenied) as ctx:
            act(self.db, actor_no_maint, driver, 'node', job['id'], 'force', confirmation=self.confirmation(job['id']))
        self.assertEqual(ctx.exception.code, 'permission_denied')

        # Member account role
        self.db.connection.execute("UPDATE backend_accounts SET role='member' WHERE id=?", (self.actor.account.id,))
        with self.assertRaises(AccessDenied) as ctx:
            act(self.db, self.actor, driver, 'node', job['id'], 'force', confirmation=self.confirmation(job['id']))
        self.assertEqual(ctx.exception.code, 'permission_denied')
        self.db.connection.execute("UPDATE backend_accounts SET role='admin' WHERE id=?", (self.actor.account.id,))

        # Disabled account status
        self.db.connection.execute("UPDATE backend_accounts SET status='disabled' WHERE id=?", (self.actor.account.id,))
        with self.assertRaises(AccessDenied) as ctx:
            act(self.db, self.actor, driver, 'node', job['id'], 'force', confirmation=self.confirmation(job['id']))
        self.assertEqual(ctx.exception.code, 'permission_denied')
        self.db.connection.execute("UPDATE backend_accounts SET status='approved' WHERE id=?", (self.actor.account.id,))

    def test_state_conflict_and_busy_rejections(self):
        job = self.queue('sync_runtime')
        driver = FakeDriver()

        # Task not blocked (currently awaiting_executor)
        with self.assertRaises(AccessDenied) as ctx:
            act(self.db, self.actor, driver, 'node', job['id'], 'force', confirmation=self.confirmation(job['id']))
        self.assertEqual(ctx.exception.code, 'task_not_blocked')
        self.assertEqual(ctx.exception.status, 409)

        # Mark blocked
        self.db.connection.execute("UPDATE backend_node_jobs SET status='blocked' WHERE id=?", (job['id'],))

        # Node already draining
        self.db.connection.execute("INSERT INTO backend_node_drains VALUES ('n1', ?, '[]', 'now')", (self.actor.account.id,))
        with self.assertRaises(AccessDenied) as ctx:
            act(self.db, self.actor, driver, 'node', job['id'], 'force', confirmation=self.confirmation(job['id']))
        self.assertEqual(ctx.exception.code, 'node_already_draining')
        self.assertEqual(ctx.exception.status, 409)
        self.db.connection.execute("DELETE FROM backend_node_drains WHERE node_key='n1'")

        # Node has running operation
        self.db.connection.execute("UPDATE backend_node_jobs SET status='blocked' WHERE id=?", (job['id'],))
        other_id = str(uuid4())
        self.db.connection.execute(
            "INSERT INTO backend_node_jobs(id, node_key, actor_id, command_key, action, revision, intent_json, status) VALUES (?, 'n1', ?, ?, 'sync_runtime', 1, '{}', 'running')",
            (other_id, self.actor.account.id, str(uuid4()))
        )
        with self.assertRaises(AccessDenied) as ctx:
            act(self.db, self.actor, driver, 'node', job['id'], 'force', confirmation=self.confirmation(job['id']))
        self.assertEqual(ctx.exception.code, 'maintenance_busy')
        self.assertEqual(ctx.exception.status, 409)

    def test_driver_prepare_receipt_errors_leave_blocked_and_audited(self):
        job = self.queue('sync_runtime')
        self.db.connection.execute("UPDATE backend_node_jobs SET status='blocked'")

        # Invalid prepare receipt (wrong ticket)
        driver = FakeDriver(prepare_receipt={'ticket_id': 'foreign-ticket', 'node_key': 'n1', 'restart_required': True})
        with self.assertRaises(AccessDenied) as ctx:
            act(self.db, self.actor, driver, 'node', job['id'], 'force', confirmation=self.confirmation(job['id']))
        self.assertEqual(ctx.exception.code, 'force_recovery_unconfirmed')

        row = self.db.connection.execute("SELECT status FROM backend_node_jobs WHERE id=?", (job['id'],)).fetchone()
        self.assertEqual(row['status'], 'blocked')

        audit = history(self.db, self.actor)['items'][0]
        self.assertEqual(audit['outcome'], 'unconfirmed')
        self.assertEqual(audit['status'], 'blocked')

    def test_driver_rpc_errors_mapped_and_leave_blocked(self):
        job = self.queue('sync_runtime')
        self.db.connection.execute("UPDATE backend_node_jobs SET status='blocked'")

        # UNIMPLEMENTED
        driver_unimpl = FakeDriver(prepare_error=FakeRpcError(grpc.StatusCode.UNIMPLEMENTED, 'unimplemented'))
        with self.assertRaises(AccessDenied) as ctx:
            act(self.db, self.actor, driver_unimpl, 'node', job['id'], 'force', confirmation=self.confirmation(job['id']))
        self.assertEqual(ctx.exception.code, 'force_agent_upgrade_required')

        # UNAVAILABLE
        driver_unavail = FakeDriver(prepare_error=FakeRpcError(grpc.StatusCode.UNAVAILABLE, 'offline'))
        with self.assertRaises(AccessDenied) as ctx:
            act(self.db, self.actor, driver_unavail, 'node', job['id'], 'force', confirmation=self.confirmation(job['id']))
        self.assertEqual(ctx.exception.code, 'force_agent_unavailable')

        # Precondition error: force_command_conflict
        driver_conflict = FakeDriver(retire_errors=[FakeRpcError(grpc.StatusCode.FAILED_PRECONDITION, 'force_command_conflict')])
        with self.assertRaises(AccessDenied) as ctx:
            act(self.db, self.actor, driver_conflict, 'node', job['id'], 'force', confirmation=self.confirmation(job['id']))
        self.assertEqual(ctx.exception.code, 'force_command_conflict')

        row = self.db.connection.execute("SELECT status FROM backend_node_jobs WHERE id=?", (job['id'],)).fetchone()
        self.assertEqual(row['status'], 'blocked')

    def test_driver_retire_retries_transient_errors_then_succeeds(self):
        job = self.queue('sync_runtime')
        self.db.connection.execute("UPDATE backend_node_jobs SET status='blocked'")

        transient_errors = [
            FakeRpcError(grpc.StatusCode.UNAVAILABLE, 'transport reset'),
            FakeRpcError(grpc.StatusCode.FAILED_PRECONDITION, 'force_restart_required'),
            FakeRpcError(grpc.StatusCode.FAILED_PRECONDITION, 'force_execution_still_running'),
        ]
        driver = FakeDriver(retire_errors=transient_errors)

        with patch('backend.force_recovery.time.sleep') as mock_sleep:
            result = act(self.db, self.actor, driver, 'node', job['id'], 'force', confirmation=self.confirmation(job['id']))

        self.assertEqual(result['status'], 'superseded')
        self.assertEqual(result['outcome'], 'force_cancelled')
        self.assertEqual(mock_sleep.call_count, 3)

    def test_idempotent_retry_after_lost_response(self):
        job = self.queue('sync_runtime')
        self.db.connection.execute("UPDATE backend_node_jobs SET status='blocked'")

        # Driver callback causing first local finalize rejection:
        # fresh account temporarily revoked during retire, while remote receipt persists.
        revoked = True

        def retire_callback(spec):
            nonlocal revoked
            if revoked:
                with self.db.transaction() as conn:
                    conn.execute("UPDATE backend_accounts SET status='disabled' WHERE id=?", (self.actor.account.id,))

        driver = FakeDriver(on_retire=retire_callback)

        # First attempt: driver prepares and retires remotely, but fresh_admin rejects local finalization
        with self.assertRaises(AccessDenied) as ctx:
            act(self.db, self.actor, driver, 'node', job['id'], 'force', confirmation=self.confirmation(job['id']))
        self.assertEqual(ctx.exception.code, 'permission_denied')

        # Task is still blocked
        row = self.db.connection.execute("SELECT status FROM backend_node_jobs WHERE id=?", (job['id'],)).fetchone()
        self.assertEqual(row['status'], 'blocked')

        # Retry while account is still revoked is blocked until authorized local finalization
        with self.assertRaises(AccessDenied) as ctx:
            act(self.db, self.actor, driver, 'node', job['id'], 'force', confirmation=self.confirmation(job['id']))
        self.assertEqual(ctx.exception.code, 'permission_denied')

        # Restore account authorization
        revoked = False
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_accounts SET status='approved' WHERE id=?", (self.actor.account.id,))

        # Retry succeeds
        result = act(self.db, self.actor, driver, 'node', job['id'], 'force', confirmation=self.confirmation(job['id']))
        self.assertEqual(result['status'], 'superseded')
        self.assertEqual(result['outcome'], 'force_cancelled')

        # Retry must use exact same spec ticket
        prepare_calls = [spec for phase, spec in driver.calls if phase == 'prepare']
        retire_calls = [spec for phase, spec in driver.calls if phase == 'retire']
        self.assertGreaterEqual(len(prepare_calls), 2)
        self.assertGreaterEqual(len(retire_calls), 2)
        spec_ticket = prepare_calls[0]['ticket_id']
        self.assertEqual(prepare_calls[1]['ticket_id'], spec_ticket)
        self.assertEqual(retire_calls[0]['ticket_id'], spec_ticket)
        self.assertEqual(retire_calls[1]['ticket_id'], spec_ticket)

        # Audit unconfirmed then force_cancelled
        audit_records = self.db.connection.execute(
            "SELECT outcome, status FROM backend_recovery_audit WHERE operation_id=? ORDER BY created_at ASC",
            (job['id'],)
        ).fetchall()
        self.assertEqual(len(audit_records), 2)
        self.assertEqual(audit_records[0]['outcome'], 'unconfirmed')
        self.assertEqual(audit_records[0]['status'], 'blocked')
        self.assertEqual(audit_records[1]['outcome'], 'force_cancelled')
        self.assertEqual(audit_records[1]['status'], 'superseded')


class ForceRecoveryHTTPTests(TestCase):
    setUp = fixture_updates.BackendUpdateTests.setUp
    service = fixture_updates.BackendUpdateTests.service

    def test_force_recovery_http_endpoint(self):
        import tempfile
        from pathlib import Path
        self.service()
        self.db.connection.execute("INSERT INTO backend_nodes(key,title,region,flag,protocols_json,xray_transports_json) VALUES ('n1','Node 1','EU','🏳️','[]','[]')")
        job_id = str(uuid4())
        self.db.connection.execute(
            "INSERT INTO backend_node_jobs VALUES (?,'n1',?,?,'sync_runtime',1,'{}','blocked',NULL)",
            (job_id, self.admin.id, str(uuid4()))
        )

        headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101'}
        fake_driver = FakeDriver()

        from backend.http_api import create_app
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as root, patch.dict('os.environ', {'NODE_PLANE_SHARED_DIR': root}):
            (Path(root) / 'data').mkdir()
            client = TestClient(create_app(self.db, node_driver=fake_driver))

            # Missing body -> 422
            resp = client.post(f'/api/v1/system/recovery/node/{job_id}/force', headers=headers)
            self.assertEqual(resp.status_code, 422)

            # Extra forbidden field -> 422
            resp = client.post(
                f'/api/v1/system/recovery/node/{job_id}/force',
                headers=headers,
                json={'confirm_operation_id': job_id, 'acknowledge_unknown_outcome': True, 'extra': 'not_allowed'}
            )
            self.assertEqual(resp.status_code, 422)

            # Valid body -> 200
            resp = client.post(
                f'/api/v1/system/recovery/node/{job_id}/force',
                headers=headers,
                json={'confirm_operation_id': job_id, 'acknowledge_unknown_outcome': True}
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            data = resp.json()
            self.assertEqual(data['status'], 'superseded')
            self.assertEqual(data['outcome'], 'force_cancelled')
            self.assertEqual(data['id'], job_id)
            self.assertEqual(data['observation'], {'forced': True, 'outcome_unknown': True})

