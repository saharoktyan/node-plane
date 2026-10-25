"""Narrowly scoped missing recovery regression tests and safety invariants."""
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID, uuid4, uuid5

from backend.authorization import AccessDenied, Actor, Principal, PrincipalKind, ADMIN_PERMISSIONS
from backend.agent_rollout import AgentRolloutService
from backend.node_operations import NodeOperations
from backend.recovery import overview, act
from backend.updates import UpdateService
from tests import test_backend_http as fixture_http
from tests import test_backend_updates as fixture_updates


class UpdateRecoverySafetyFollowupTests(unittest.TestCase):
    setUp = fixture_http.BackendHTTPTests.setUp
    service = fixture_updates.BackendUpdateTests.service
    node = fixture_updates.BackendUpdateTests.node

    def gate(self):
        with self.db.connect() as conn:
            return conn.execute('SELECT job_id FROM backend_controller_update_gate WHERE id=1').fetchone()

    def test_unknown_blocked_stack_core_without_result_json_or_zero_items_cannot_cancel_or_resume_and_keeps_gate(self):
        service = self.service()
        job = service.queue(self.actor, str(uuid4()), 'stack', target_ref='v0.4.3-alpha.19', branch='dev')
        service.run_one()
        self.assertIsNotNone(self.gate())

        # Subcase A: stack core blocked with result_json NULL
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_update_jobs SET status='blocked', result_json=NULL WHERE id=?", (job['id'],))

        with self.assertRaises(AccessDenied) as ctx:
            service.cancel(self.actor, job['id'])
        self.assertEqual(ctx.exception.code, 'update_cancel_unsafe')
        self.assertIsNotNone(self.gate())

        with self.assertRaises(AccessDenied) as ctx:
            service.resume(self.actor, job['id'])
        self.assertIn(ctx.exception.code, {'update_recovery_unavailable', 'recovery_unconfirmed'})
        self.assertIsNotNone(self.gate())

        # Subcase B: stack core blocked with result_json = '{}' and zero node items
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_update_jobs SET status='blocked', result_json='{}' WHERE id=?", (job['id'],))
            conn.execute("DELETE FROM backend_update_items WHERE job_id=?", (job['id'],))

        with self.assertRaises(AccessDenied) as ctx:
            service.cancel(self.actor, job['id'])
        self.assertEqual(ctx.exception.code, 'update_cancel_unsafe')
        self.assertIsNotNone(self.gate())

        with self.assertRaises(AccessDenied) as ctx:
            service.resume(self.actor, job['id'])
        self.assertIn(ctx.exception.code, {'update_recovery_unavailable', 'recovery_unconfirmed'})
        self.assertIsNotNone(self.gate())

    def test_active_missing_or_unrelated_version_unit_cannot_cancel(self):
        service = self.service()
        job = service.queue(self.actor, str(uuid4()), 'version', target_ref='v0.4.3-alpha.19', branch='dev')

        # Subcase A: active / running version update cannot be cancelled
        service.run_one()
        self.assertEqual(service.get(self.actor, job['id'])['status'], 'running')
        with self.assertRaises(AccessDenied) as ctx:
            service.cancel(self.actor, job['id'])
        self.assertEqual(ctx.exception.code, 'update_cancel_unsafe')

        # Subcase B: blocked version update with missing unit evidence
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_update_jobs SET status='blocked', result_json=NULL WHERE id=?", (job['id'],))
        service.updater.refresh_update_run_state.return_value = {
            'last_run_status': 'failed', 'last_run_unit': None
        }
        with self.assertRaises(AccessDenied) as ctx:
            service.cancel(self.actor, job['id'])
        self.assertEqual(ctx.exception.code, 'update_cancel_unsafe')

        # Subcase C: blocked version update with unrelated unit
        plan = json.dumps({'status': 'blocked', 'unit_name': 'expected-version-unit', 'resolved_ref': 'v0.4.3-alpha.19'})
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_update_jobs SET status='blocked', result_json=? WHERE id=?", (plan, job['id']))
        service.updater.refresh_update_run_state.return_value = {
            'last_run_status': 'failed', 'last_run_unit': 'unrelated-version-unit'
        }
        with self.assertRaises(AccessDenied) as ctx:
            service.cancel(self.actor, job['id'])
        self.assertEqual(ctx.exception.code, 'update_cancel_unsafe')

    def test_exact_bound_child_adoption_rejects_colliding_or_wrong_child(self):
        service = self.service()
        self.node()
        service.driver.inspect_node_services.return_value = {'agent_commit': 'old' * 10, 'runtime_commit': 'a' * 40}
        with patch('config.APP_COMMIT', 'a' * 40):
            job = service.queue(self.actor, str(uuid4()), 'agents')

            # Compute the expected command_key for item lv1:agents
            expected_key = str(uuid5(UUID(job['id']), 'lv1:agents'))

            # Insert another node 'lv2'
            with self.db.transaction() as conn:
                conn.execute("INSERT INTO backend_nodes(key,title,region,enabled,applied_revision,desired_revision,protocols_json,xray_transports_json) VALUES ('lv2','Node2','EU',0,1,1,'[]','[]')")
                conn.execute("INSERT INTO backend_node_connections(node_key,transport,ssh_target) VALUES ('lv2','ssh','root@lv2.example')")

            # Create an orphan rollout having the same command_key BUT bound to colliding wrong node 'lv2'
            wrong_child = AgentRolloutService(self.db).request(self.actor, 'lv2', expected_key, transport='ssh', ssh_target='root@lv2.example')
            with self.db.transaction() as conn:
                conn.execute("UPDATE backend_agent_rollouts SET status='succeeded' WHERE id=?", (wrong_child['id'],))
                conn.execute("UPDATE backend_update_jobs SET status='blocked' WHERE id=?", (job['id'],))
                conn.execute("UPDATE backend_update_items SET status='blocked', child_id=NULL WHERE job_id=?", (job['id'],))

            # Recheck must reject adopting the wrong/colliding child for lv1
            service.driver.inspect_node_services.return_value = {'agent_commit': 'a' * 40, 'runtime_commit': 'a' * 40}
            with self.assertRaises(AccessDenied) as ctx:
                service.recheck(self.actor, job['id'])
            self.assertEqual(ctx.exception.code, 'update_child_mismatch')

    def test_replacement_uuid_key_consistent_during_orphan_adoption_after_fenced_retry(self):
        service = self.service()
        self.node()
        service.driver.inspect_node_services.return_value = {'agent_commit': 'old' * 10, 'runtime_commit': 'a' * 40}
        with patch('config.APP_COMMIT', 'a' * 40):
            job = service.queue(self.actor, str(uuid4()), 'agents')
            service.run_one()
            item = service.get(self.actor, job['id'])['items'][0]
            old_child_id = item['child_id']
            self.assertIsNotNone(old_child_id)

            # Old child is fenced/cancelled
            with self.db.transaction() as conn:
                conn.execute("UPDATE backend_agent_rollouts SET status='cancelled' WHERE id=?", (old_child_id,))
                conn.execute("UPDATE backend_update_jobs SET status='blocked' WHERE id=?", (job['id'],))
                conn.execute("UPDATE backend_update_items SET status='blocked' WHERE job_id=?", (job['id'],))

            # Resume retires old child and records retired_child_id
            service.resume(self.actor, job['id'])
            expected_replacement_key = str(uuid5(UUID(job['id']), 'agents:' + old_child_id))

            # Simulate orphan creation of replacement child before parent saved child_id
            replacement_child = AgentRolloutService(self.db).request(self.actor, 'lv1', expected_replacement_key, transport='ssh', ssh_target='root@lv1.example')
            with self.db.transaction() as conn:
                conn.execute("UPDATE backend_agent_rollouts SET status='succeeded' WHERE id=?", (replacement_child['id'],))
                conn.execute("UPDATE backend_update_jobs SET status='blocked' WHERE id=?", (job['id'],))
                conn.execute("UPDATE backend_update_items SET status='blocked', child_id=NULL WHERE job_id=?", (job['id'],))

            service.driver.inspect_node_services.return_value = {'agent_commit': 'a' * 40, 'runtime_commit': 'a' * 40}
            rechecked = service.recheck(self.actor, job['id'])
            # Recheck must adopt the replacement child by its consistent replacement key
            self.assertEqual(rechecked['items'][0]['child_id'], replacement_child['id'])
            self.assertEqual(rechecked['status'], 'succeeded')

    def test_stack_agents_phase_resume_cannot_execute_different_current_controller_commit(self):
        service = self.service()
        self.node()
        service.driver.inspect_node_services.return_value = {'agent_commit': 'old' * 10, 'runtime_commit': 'a' * 40}
        with patch('config.APP_COMMIT', 'a' * 40):
            job = service.queue(self.actor, str(uuid4()), 'stack', target_ref='v0.4.3-alpha.19', branch='dev')
            # Complete core phase and transition to agents phase
            with self.db.transaction() as conn:
                conn.execute("UPDATE backend_update_jobs SET status='blocked', result_json=? WHERE id=?",
                    (json.dumps({'phase': 'agents', 'status': 'running', 'unit_name': 'test-unit'}), job['id']))
                conn.execute("UPDATE backend_update_items SET status='blocked' WHERE job_id=?", (job['id'],))

            # Controller commit has drifted to a newer commit
            with patch('config.APP_COMMIT', 'c' * 40):
                with self.assertRaises(AccessDenied) as ctx:
                    service.resume(self.actor, job['id'])
                self.assertEqual(ctx.exception.code, 'update_target_drifted')

    def test_update_cancel_and_resume_never_perform_rpcs_inside_db_transaction_and_child_must_be_resolved_first(self):
        service = self.service()
        self.node()
        service.driver.inspect_node_services.return_value = {'agent_commit': 'old' * 10, 'runtime_commit': 'a' * 40}
        with patch('config.APP_COMMIT', 'a' * 40):
            job = service.queue(self.actor, str(uuid4()), 'agents')
            service.run_one()
            item = service.get(self.actor, job['id'])['items'][0]
            child_id = item['child_id']
            self.assertIsNotNone(child_id)

            # Child is in unconfirmed/blocked state
            with self.db.transaction() as conn:
                conn.execute("UPDATE backend_agent_rollouts SET status='blocked' WHERE id=?", (child_id,))
                conn.execute("UPDATE backend_update_jobs SET status='blocked' WHERE id=?", (job['id'],))
                conn.execute("UPDATE backend_update_items SET status='blocked' WHERE job_id=?", (job['id'],))

            # Spy on driver calls: no driver RPC should be called while child is unconfirmed
            driver_mock = Mock(wraps=service.driver)
            service.driver = driver_mock

            # Cancel must reject because child is unknown/unsafe without executing remote RPCs
            with self.assertRaises(AccessDenied) as ctx:
                service.cancel(self.actor, job['id'])
            self.assertEqual(ctx.exception.code, 'update_cancel_unsafe')
            driver_mock.inspect_node_services.assert_not_called()

            # Resume must reject because child is unknown/unsafe without executing remote RPCs
            with self.assertRaises(AccessDenied) as ctx:
                service.resume(self.actor, job['id'])
            self.assertEqual(ctx.exception.code, 'update_child_recovery_required')
            driver_mock.inspect_node_services.assert_not_called()

            # Now explicitly resolve/fence the child first
            with self.db.transaction() as conn:
                conn.execute("UPDATE backend_agent_rollouts SET status='cancelled' WHERE id=?", (child_id,))

            # After child is explicitly resolved/fenced, parent resume succeeds
            resumed = service.resume(self.actor, job['id'])
            self.assertEqual(resumed['status'], 'running')

    def test_no_driver_mutation_occurs_on_read_job_cancellation(self):
        self.node()
        actor = Actor(Principal('admin', PrincipalKind.ACCOUNT, ADMIN_PERMISSIONS), self.admin)
        # Create a blocked config issuance (read-only artifact job)
        issuance_id = str(uuid4())
        profile_id = str(uuid4())
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_profiles(id,runtime_name,display_name,created_at) VALUES (?, 'prof1', 'Profile 1', 'now')",
                (profile_id,))
            conn.execute("INSERT INTO backend_config_issuances(id,actor_account_id,command_key,profile_id,node_key,protocol,transport,profile_revision,node_revision,status,expires_at,created_at) VALUES (?,?,?,?,'lv1','xray','tcp',1,1,'blocked','2099-01-01T00:00:00Z','now')",
                (issuance_id, self.admin.id, str(uuid4()), profile_id))

        # Overview offers cancel
        items = overview(self.db, actor)['items']
        item = next(i for i in items if i['id'] == issuance_id)
        self.assertEqual(item['kind'], 'issuance')
        self.assertEqual(item['actions'], ['cancel'])

        # Driver spy
        driver_mock = Mock()
        act(self.db, actor, driver_mock, 'issuance', issuance_id, 'cancel')

        # Status is superseded
        with self.db.connect() as conn:
            row = conn.execute("SELECT status FROM backend_config_issuances WHERE id=?", (issuance_id,)).fetchone()
            self.assertEqual(row['status'], 'superseded')

        # No driver mutation occurred
        driver_mock.node_action.assert_not_called()
        self.assertEqual(len(driver_mock.method_calls), 0)

    def test_api_recovery_new_issuance_cleanup_kinds_and_injection_rejection(self):
        self.node()
        actor = Actor(Principal('admin', PrincipalKind.ACCOUNT, ADMIN_PERMISSIONS), self.admin)
        driver = Mock()
        issuance_id = str(uuid4())
        profile_id = str(uuid4())
        cleanup_id = str(uuid4())

        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_profiles(id,runtime_name,display_name,created_at) VALUES (?, 'prof2', 'Profile 2', 'now')",
                (profile_id,))
            conn.execute("INSERT INTO backend_config_issuances(id,actor_account_id,command_key,profile_id,node_key,protocol,transport,profile_revision,node_revision,status,expires_at,created_at) VALUES (?,?,?,?,'lv1','xray','tcp',1,1,'blocked','2099-01-01T00:00:00Z','now')",
                (issuance_id, self.admin.id, str(uuid4()), profile_id))
            conn.execute("INSERT INTO backend_system_cleanup_jobs(id,actor_id,principal_id,command_key,plan_id,intent_json,phase,status,created_at) VALUES (?,?,?,?,?,?,'nodes','blocked','now')",
                (cleanup_id, self.admin.id, 'admin', str(uuid4()), str(uuid4()), json.dumps({'action': 'reset', 'cleanup_nodes': False, 'node_keys': []})))

        items = overview(self.db, actor)['items']
        cleanup_item = next(i for i in items if i['id'] == cleanup_id)
        self.assertEqual(cleanup_item['kind'], 'cleanup')
        self.assertIn('retry', cleanup_item['actions'])

        # Invalid action pairs rejected
        with self.assertRaises(AccessDenied):
            act(self.db, actor, driver, 'issuance', issuance_id, 'retry')
        with self.assertRaises(AccessDenied):
            act(self.db, actor, driver, 'issuance', issuance_id, 'resolve')
        with self.assertRaises(AccessDenied):
            act(self.db, actor, driver, 'issuance', issuance_id, 'recheck')

        # Injections rejected with 422 invalid_input
        for bad_id in ('../injection', 'not_a_uuid', 'foo;bar'):
            with self.assertRaises(AccessDenied) as ctx:
                act(self.db, actor, driver, 'issuance', bad_id, 'cancel')
            self.assertEqual(ctx.exception.code, 'invalid_input')

        # Unknown kinds rejected with 422
        with self.assertRaises(AccessDenied) as ctx:
            act(self.db, actor, driver, 'unknown_kind', issuance_id, 'cancel')
        self.assertEqual(ctx.exception.code, 'invalid_input')

    def test_version_predispatch_binding_saved_before_scheduler_and_predispatch_rejection_releases_gate(self):
        service = self.service()
        job = service.queue(self.actor, str(uuid4()), 'version', target_ref='v0.4.3-alpha.19', branch='dev')
        expected_unit = f"node-plane-update-{UUID(job['id'])}"

        spied = {}

        def spy_schedule_update(**kwargs):
            self.assertEqual(kwargs.get('launch_job_id'), job['id'])
            with self.db.connect() as conn:
                row = conn.execute("SELECT status, result_json FROM backend_update_jobs WHERE id=?", (job['id'],)).fetchone()
            spied['status'] = row['status']
            plan = json.loads(row['result_json'])
            spied['unit_name'] = plan.get('unit_name')
            spied['launch_state'] = plan.get('launch_state')
            return {'status': 'failed', 'launch_state': 'not_started', 'unit_name': expected_unit}

        service.updater.schedule_update = Mock(side_effect=spy_schedule_update)
        service.run_one()

        # Binding was saved to SQLite BEFORE scheduler was invoked
        self.assertEqual(spied['status'], 'running')
        self.assertEqual(spied['unit_name'], expected_unit)
        self.assertEqual(spied['launch_state'], 'dispatching')

        # Job is blocked with proven not_started launch_state
        job_state = service.get(self.actor, job['id'])
        self.assertEqual(job_state['status'], 'blocked')
        self.assertEqual(job_state['result']['launch_state'], 'not_started')

        # Known predispatch rejection can be safely cancelled and releases own gate
        cancelled = service.cancel(self.actor, job['id'])
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertIsNone(self.gate())

        # Positive cancellation allows next queue
        next_job = service.queue(self.actor, str(uuid4()), 'version', target_ref='v0.4.3-alpha.19', branch='dev')
        self.assertIsNotNone(next_job['id'])
        self.assertIsNotNone(self.gate())

    def test_version_uncertain_dispatch_cannot_silently_unlock_and_blocks_subsequent_queue(self):
        service = self.service()
        job = service.queue(self.actor, str(uuid4()), 'version', target_ref='v0.4.3-alpha.19', branch='dev')
        expected_unit = f"node-plane-update-{UUID(job['id'])}"
        service.updater.schedule_update = Mock(return_value={
            'status': 'failed', 'launch_state': 'dispatching', 'unit_name': expected_unit
        })
        service.run_one()
        self.assertEqual(service.get(self.actor, job['id'])['status'], 'blocked')
        self.assertIsNotNone(self.gate())
        with self.assertRaises(AccessDenied) as ctx:
            service.cancel(self.actor, job['id'])
        self.assertEqual(ctx.exception.code, 'update_cancel_unsafe')
        self.assertIsNotNone(self.gate())

        # Subsequent admission is blocked while prior core outcome is unknown
        with self.assertRaises(AccessDenied) as ctx:
            service.queue(self.actor, str(uuid4()), 'version', target_ref='v0.4.3-alpha.19', branch='dev')
        self.assertEqual(ctx.exception.code, 'update_pending')
        self.assertIsNotNone(self.gate())

    def test_version_mismatched_unit_cannot_silently_unlock_and_blocks_subsequent_queue(self):
        service = self.service()
        job = service.queue(self.actor, str(uuid4()), 'version', target_ref='v0.4.3-alpha.19', branch='dev')
        service.updater.schedule_update = Mock(return_value={
            'status': 'failed', 'unit_name': 'unrelated-unit'
        })
        service.run_one()
        self.assertEqual(service.get(self.actor, job['id'])['status'], 'blocked')
        self.assertIsNotNone(self.gate())
        with self.assertRaises(AccessDenied) as ctx:
            service.cancel(self.actor, job['id'])
        self.assertEqual(ctx.exception.code, 'update_cancel_unsafe')
        self.assertIsNotNone(self.gate())

        # Subsequent admission is blocked while prior core outcome is unknown
        with self.assertRaises(AccessDenied) as ctx:
            service.queue(self.actor, str(uuid4()), 'version', target_ref='v0.4.3-alpha.19', branch='dev')
        self.assertEqual(ctx.exception.code, 'update_pending')
        self.assertIsNotNone(self.gate())

    def test_version_missing_unit_cannot_silently_unlock_and_blocks_subsequent_queue(self):
        service = self.service()
        job = service.queue(self.actor, str(uuid4()), 'version', target_ref='v0.4.3-alpha.19', branch='dev')
        expected_unit = f"node-plane-update-{UUID(job['id'])}"
        service.updater.schedule_update = Mock(return_value={'status': 'running', 'unit_name': expected_unit})
        service.run_one()  # launches job -> running
        self.assertEqual(service.get(self.actor, job['id'])['status'], 'running')
        self.assertIsNotNone(self.gate())

        # Unit observation reports missing
        service.updater.refresh_update_run_state.return_value = {
            'last_run_status': 'running', 'last_run_unit': expected_unit, 'last_run_unit_missing': True
        }
        service.run_one()  # transitions to blocked due to missing unit
        self.assertEqual(service.get(self.actor, job['id'])['status'], 'blocked')
        self.assertIsNotNone(self.gate())
        with self.assertRaises(AccessDenied) as ctx:
            service.cancel(self.actor, job['id'])
        self.assertEqual(ctx.exception.code, 'update_cancel_unsafe')
        self.assertIsNotNone(self.gate())

        # Subsequent admission is blocked while prior core outcome is unknown
        with self.assertRaises(AccessDenied) as ctx:
            service.queue(self.actor, str(uuid4()), 'version', target_ref='v0.4.3-alpha.19', branch='dev')
        self.assertEqual(ctx.exception.code, 'update_pending')
        self.assertIsNotNone(self.gate())

    def test_stack_predispatch_binding_saved_before_scheduler_and_predispatch_rejection_releases_gate(self):
        service = self.service()
        job = service.queue(self.actor, str(uuid4()), 'stack', target_ref='v0.4.3-alpha.19', branch='dev')
        expected_unit = f"node-plane-update-{UUID(job['id'])}"
        self.assertIsNotNone(self.gate())

        spied = {}

        def spy_schedule_update(**kwargs):
            self.assertEqual(kwargs.get('stack_job_id'), job['id'])
            with self.db.connect() as conn:
                row = conn.execute("SELECT status, result_json FROM backend_update_jobs WHERE id=?", (job['id'],)).fetchone()
            spied['status'] = row['status']
            plan = json.loads(row['result_json'])
            spied['unit_name'] = plan.get('unit_name')
            spied['launch_state'] = plan.get('launch_state')
            return {'status': 'failed', 'launch_state': 'not_started', 'unit_name': expected_unit}

        service.updater.schedule_update = Mock(side_effect=spy_schedule_update)
        service.run_one()

        # Binding was saved to SQLite BEFORE scheduler was invoked
        self.assertEqual(spied['status'], 'running')
        self.assertEqual(spied['unit_name'], expected_unit)
        self.assertEqual(spied['launch_state'], 'dispatching')

        # Core phase is blocked with proven not_started launch_state
        job_state = service.get(self.actor, job['id'])
        self.assertEqual(job_state['status'], 'blocked')
        self.assertEqual(job_state['result']['launch_state'], 'not_started')
        self.assertIsNotNone(self.gate())

        # Known predispatch rejection can be safely cancelled and releases own gate
        cancelled = service.cancel(self.actor, job['id'])
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertIsNone(self.gate())

        # Positive cancellation allows next queue
        next_job = service.queue(self.actor, str(uuid4()), 'stack', target_ref='v0.4.3-alpha.19', branch='dev')
        self.assertIsNotNone(next_job['id'])
        self.assertIsNotNone(self.gate())

    def test_stack_uncertain_dispatch_cannot_silently_unlock_and_blocks_subsequent_queue(self):
        service = self.service()
        job = service.queue(self.actor, str(uuid4()), 'stack', target_ref='v0.4.3-alpha.19', branch='dev')
        expected_unit = f"node-plane-update-{UUID(job['id'])}"
        service.updater.schedule_update = Mock(return_value={
            'status': 'failed', 'launch_state': 'dispatching', 'unit_name': expected_unit
        })
        service.run_one()
        self.assertEqual(service.get(self.actor, job['id'])['status'], 'blocked')
        self.assertIsNotNone(self.gate())
        with self.assertRaises(AccessDenied) as ctx:
            service.cancel(self.actor, job['id'])
        self.assertEqual(ctx.exception.code, 'update_cancel_unsafe')
        self.assertIsNotNone(self.gate())

        # Subsequent admission is blocked while prior core outcome is unknown
        with self.assertRaises(AccessDenied) as ctx:
            service.queue(self.actor, str(uuid4()), 'stack', target_ref='v0.4.3-alpha.19', branch='dev')
        self.assertEqual(ctx.exception.code, 'update_pending')
        self.assertIsNotNone(self.gate())

    def test_stack_mismatched_unit_cannot_silently_unlock_and_blocks_subsequent_queue(self):
        service = self.service()
        job = service.queue(self.actor, str(uuid4()), 'stack', target_ref='v0.4.3-alpha.19', branch='dev')
        service.updater.schedule_update = Mock(return_value={
            'status': 'failed', 'unit_name': 'unrelated-unit'
        })
        service.run_one()
        self.assertEqual(service.get(self.actor, job['id'])['status'], 'blocked')
        self.assertIsNotNone(self.gate())
        with self.assertRaises(AccessDenied) as ctx:
            service.cancel(self.actor, job['id'])
        self.assertEqual(ctx.exception.code, 'update_cancel_unsafe')
        self.assertIsNotNone(self.gate())

        # Subsequent admission is blocked while prior core outcome is unknown
        with self.assertRaises(AccessDenied) as ctx:
            service.queue(self.actor, str(uuid4()), 'stack', target_ref='v0.4.3-alpha.19', branch='dev')
        self.assertEqual(ctx.exception.code, 'update_pending')
        self.assertIsNotNone(self.gate())
