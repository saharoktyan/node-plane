"""Bounded update recovery regression tests and invariants validation."""
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID, uuid4, uuid5

from backend.authorization import AccessDenied
from backend.agent_rollout import AgentRolloutService
from backend.node_operations import NodeOperations
from backend.recovery import overview, act
from backend.updates import UpdateService
from tests import test_backend_updates as fixtures


class RecoveryFollowupTests(unittest.TestCase):
    setUp = fixtures.BackendUpdateTests.setUp
    service = fixtures.BackendUpdateTests.service
    node = fixtures.BackendUpdateTests.node

    def test_legacy_blocked_version_recheck_verifies_build_and_never_launches_install(self):
        service = self.service()
        job = service.queue(self.actor, str(uuid4()), 'version', target_ref='v0.4.3-alpha.19', branch='dev')
        service.run_one()
        self.assertEqual(service.updater.schedule_update.call_count, 1)

        # Existing legacy launches have no durable core receipt/phase.
        with self.db.transaction() as conn:
            row = conn.execute('SELECT result_json FROM backend_update_jobs WHERE id=?', (job['id'],)).fetchone()
            plan = json.loads(row['result_json'])
            plan.pop('phase', None)
            conn.execute("UPDATE backend_update_jobs SET status='blocked',result_json=? WHERE id=?", (json.dumps(plan), job['id']))

        # Recheck with unit mismatch (missing evidence)
        service.updater.refresh_update_run_state.return_value = {
            'last_run_status': 'success', 'last_run_unit': 'different-unit'
        }
        with self.assertRaises(AccessDenied) as denied:
            service.recheck(self.actor, job['id'])
        self.assertEqual(denied.exception.code, 'update_recovery_unconfirmed')

        # Recheck with matching unit but wrong build commit
        service.updater.refresh_update_run_state.return_value = {
            'last_run_status': 'success', 'last_run_unit': f"node-plane-update-{job['id']}"
        }
        service.driver.binary_info.return_value = {'commit': 'w' * 40}
        rechecked = service.recheck(self.actor, job['id'])
        self.assertEqual(rechecked['status'], 'blocked')
        self.assertEqual(rechecked['result']['error_code'], 'build_verification_failed')

        # Recheck with matching unit and verified build
        service.driver.binary_info.return_value = {'commit': 'a' * 40}
        rechecked = service.recheck(self.actor, job['id'])
        self.assertEqual(rechecked['status'], 'succeeded')

        # schedule_update was NEVER called again during any recheck
        self.assertEqual(service.updater.schedule_update.call_count, 1)

    def test_blocked_agents_recheck_adopts_orphaned_child_by_deterministic_key(self):
        service = self.service()
        self.node()
        service.driver.inspect_node_services.return_value = {'agent_commit': 'x' * 40, 'runtime_commit': 'a' * 40}
        with patch('config.APP_COMMIT', 'a' * 40):
            job = service.queue(self.actor, str(uuid4()), 'agents')

            child_key = str(uuid5(UUID(job['id']), 'lv1:agents'))

            # Simulate crash: AgentRollout was requested with deterministic key, but link in parent items not yet saved
            child = AgentRolloutService(self.db).request(self.actor, 'lv1', child_key, transport='ssh', ssh_target='root@lv1.example')
            with self.db.transaction() as conn:
                conn.execute("UPDATE backend_update_jobs SET status='blocked' WHERE id=?", (job['id'],))
                conn.execute("UPDATE backend_update_items SET status='blocked', child_id=NULL WHERE job_id=?", (job['id'],))

            # Child completes on remote host, build verified
            with self.db.transaction() as conn:
                conn.execute("UPDATE backend_agent_rollouts SET status='succeeded' WHERE id=?", (child['id'],))
            service.driver.inspect_node_services.return_value = {'agent_commit': 'a' * 40, 'runtime_commit': 'a' * 40}

            # Recheck adopts child and updates parent
            rechecked = service.recheck(self.actor, job['id'])
            self.assertEqual(rechecked['status'], 'succeeded')
            self.assertEqual(rechecked['items'][0]['child_id'], child['id'])
            self.assertEqual(rechecked['items'][0]['status'], 'succeeded')

    def test_child_outcome_truthfulness_partial_cancelled_never_unconfirmed_success(self):
        service = self.service()
        # Create two installed nodes
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_nodes(key,title,region,enabled,applied_revision,desired_revision,protocols_json,xray_transports_json) VALUES ('n1','Node1','EU',0,1,1,'[]','[]')")
            conn.execute("INSERT INTO backend_node_connections(node_key,transport,ssh_target) VALUES ('n1','ssh','root@n1.example')")
            conn.execute("INSERT INTO backend_nodes(key,title,region,enabled,applied_revision,desired_revision,protocols_json,xray_transports_json) VALUES ('n2','Node2','EU',0,1,1,'[]','[]')")
            conn.execute("INSERT INTO backend_node_connections(node_key,transport,ssh_target) VALUES ('n2','ssh','root@n2.example')")

        service.driver.inspect_node_services.return_value = {'agent_commit': 'old' * 10, 'runtime_commit': 'a' * 40}
        with patch('config.APP_COMMIT', 'a' * 40):
            job = service.queue(self.actor, str(uuid4()), 'agents')

            service.run_one()
            service.run_one()
            items = service.get(self.actor, job['id'])['items']
            self.assertEqual(len(items), 2)

            # n1 child succeeds, n2 child is cancelled
            with self.db.transaction() as conn:
                conn.execute("UPDATE backend_agent_rollouts SET status='succeeded' WHERE id=?", (items[0]['child_id'],))
                conn.execute("UPDATE backend_agent_rollouts SET status='cancelled' WHERE id=?", (items[1]['child_id'],))
                conn.execute("UPDATE backend_update_jobs SET status='blocked' WHERE id=?", (job['id'],))

            service.driver.inspect_node_services.return_value = {'agent_commit': 'a' * 40, 'runtime_commit': 'a' * 40}
            rechecked = service.recheck(self.actor, job['id'])

            # Finished truthfully: partial (not succeeded!)
            self.assertEqual(rechecked['status'], 'partial')
            statuses = {i['status'] for i in rechecked['items']}
            self.assertEqual(statuses, {'succeeded', 'cancelled'})

    def test_driver_retry_allowed_only_after_normally_returned_installer_failure(self):
        service = self.service()
        with patch('config.APP_COMMIT', 'b' * 40):
            # Normal returned failure (exit code != 0)
            service.runner = Mock(return_value=False)
            job1 = service.queue(self.actor, str(uuid4()), 'agents')

            with patch.dict(os.environ, {'NODE_PLANE_APP_DIR': '/opt/node-plane/current'}):
                service.run_one()
                service.run_one()

            state1 = service.get(self.actor, job1['id'])
            self.assertEqual(state1['status'], 'blocked')
            self.assertEqual(state1['items'][0]['error_code'], 'driver_install_failed')

            # Resume is allowed after driver_install_failed
            resumed = service.resume(self.actor, job1['id'])
            self.assertEqual(resumed['status'], 'running')
            self.assertEqual(resumed['items'][0]['status'], 'awaiting_executor')

            # Clean up job1 gate for job2
            with self.db.transaction() as conn:
                conn.execute("UPDATE backend_update_jobs SET status='cancelled' WHERE id=?", (job1['id'],))
                conn.execute("DELETE FROM backend_controller_update_gate")

            # Timeout / crash / interrupted execution
            service.runner = Mock(side_effect=TimeoutError("installer timeout"))
            job2 = service.queue(self.actor, str(uuid4()), 'agents')

            with patch.dict(os.environ, {'NODE_PLANE_APP_DIR': '/opt/node-plane/current'}):
                service.run_one()
                service.run_one()

            state2 = service.get(self.actor, job2['id'])
            self.assertEqual(state2['status'], 'blocked')
            self.assertEqual(state2['items'][0]['error_code'], 'driver_install_unconfirmed')

            # Resume is refused after driver_install_unconfirmed
            with self.assertRaises(AccessDenied) as denied:
                service.resume(self.actor, job2['id'])
            self.assertEqual(denied.exception.code, 'update_recovery_unconfirmed')

    def test_driver_recheck_verifies_installed_commit_to_settle_old_item(self):
        service = self.service()
        with patch('config.APP_COMMIT', 'b' * 40):
            job = service.queue(self.actor, str(uuid4()), 'agents')

        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_update_jobs SET status='blocked' WHERE id=?", (job['id'],))
            conn.execute("UPDATE backend_update_items SET status='blocked', error_code='driver_install_unconfirmed' WHERE job_id=?", (job['id'],))

        # Host inspection shows driver binary actually matches expected commit
        service.driver.binary_info.return_value = {'commit': 'b' * 40}
        rechecked = service.recheck(self.actor, job['id'])
        self.assertEqual(rechecked['status'], 'succeeded')
        self.assertEqual(rechecked['items'][0]['status'], 'succeeded')
        self.assertIsNone(rechecked['items'][0]['error_code'])

    def test_explicit_resume_replaces_only_after_old_child_is_fenced_with_deterministic_identity(self):
        service = self.service()
        self.node()
        service.driver.inspect_node_services.return_value = {'agent_commit': 'old' * 10, 'runtime_commit': 'a' * 40}
        with patch('config.APP_COMMIT', 'a' * 40):
            job = service.queue(self.actor, str(uuid4()), 'agents')

            service.run_one()
            item = service.get(self.actor, job['id'])['items'][0]
            old_child_id = item['child_id']
            self.assertIsNotNone(old_child_id)

            # Child is fenced / cancelled
            with self.db.transaction() as conn:
                conn.execute("UPDATE backend_agent_rollouts SET status='cancelled' WHERE id=?", (old_child_id,))
                conn.execute("UPDATE backend_update_jobs SET status='blocked' WHERE id=?", (job['id'],))
                conn.execute("UPDATE backend_update_items SET status='blocked' WHERE job_id=?", (job['id'],))

            resumed = service.resume(self.actor, job['id'])
            self.assertEqual(resumed['status'], 'running')
            self.assertEqual(resumed['items'][0]['status'], 'awaiting_executor')

            # Next worker dispatch creates replacement bound to retired child
            service.run_one()
            updated_item = service.get(self.actor, job['id'])['items'][0]
            new_child_id = updated_item['child_id']
            self.assertNotEqual(new_child_id, old_child_id)

            # Deterministic identity matches uuid5(job_id, phase + ':' + old_child_id)
            expected_new_key = str(uuid5(UUID(job['id']), 'agents:' + old_child_id))
            with self.db.connect() as conn:
                new_child = conn.execute("SELECT * FROM backend_agent_rollouts WHERE id=?", (new_child_id,)).fetchone()
                self.assertEqual(new_child['command_key'], expected_new_key)

    def test_cancellation_allowed_only_when_execution_boundaries_proven_safe(self):
        service = self.service()
        self.node()
        service.driver.inspect_node_services.return_value = {'agent_commit': 'old' * 10, 'runtime_commit': 'a' * 40}
        with patch('config.APP_COMMIT', 'a' * 40):
            job = service.queue(self.actor, str(uuid4()), 'agents')

        service.run_one()
        item = service.get(self.actor, job['id'])['items'][0]
        child_id = item['child_id']

        # Child is in unconfirmed blocked state (not preflight-failed)
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_agent_rollouts SET status='blocked' WHERE id=?", (child_id,))
            conn.execute("UPDATE backend_update_jobs SET status='blocked' WHERE id=?", (job['id'],))
            conn.execute("UPDATE backend_update_items SET status='blocked' WHERE job_id=?", (job['id'],))

        # Cancel must be denied because child is unknown/unsafe
        with self.assertRaises(AccessDenied) as denied:
            service.cancel(self.actor, job['id'])
        self.assertEqual(denied.exception.code, 'update_cancel_unsafe')

        # Record preflight failure code so child is proven safe before host changes
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_agent_rollout_failures VALUES (?,'rust_required')", (child_id,))

        # Now cancellation succeeds
        cancelled = service.cancel(self.actor, job['id'])
        self.assertEqual(cancelled['status'], 'cancelled')
        with self.db.connect() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM backend_controller_update_gate WHERE id=1").fetchone())

    def test_silently_newer_controller_commit_cannot_execute_older_update_intent(self):
        service = self.service()
        self.node()
        service.driver.inspect_node_services.return_value = {'agent_commit': 'old' * 10, 'runtime_commit': 'a' * 40}
        with patch('config.APP_COMMIT', 'a' * 40):
            job = service.queue(self.actor, str(uuid4()), 'agents')

        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_update_jobs SET status='blocked' WHERE id=?", (job['id'],))
            conn.execute("UPDATE backend_update_items SET status='blocked' WHERE job_id=?", (job['id'],))

        # Controller commit drifts to newer commit 'c'*40
        with patch('config.APP_COMMIT', 'c' * 40):
            with self.assertRaises(AccessDenied) as denied:
                service.resume(self.actor, job['id'])
            self.assertEqual(denied.exception.code, 'update_target_drifted')

    def test_real_queued_agents_failure_then_recovery_then_next_operation(self):
        service = self.service()
        self.node()
        service.driver.inspect_node_services.return_value = {'agent_commit': 'old' * 10, 'runtime_commit': 'a' * 40}

        with patch('config.APP_COMMIT', 'a' * 40):
            job = service.queue(self.actor, str(uuid4()), 'agents')
            service.run_one()
            item = service.get(self.actor, job['id'])['items'][0]
            child_id = item['child_id']

            # Simulate agent preflight failure
            with self.db.transaction() as conn:
                conn.execute("UPDATE backend_agent_rollouts SET status='blocked' WHERE id=?", (child_id,))
                conn.execute("INSERT INTO backend_agent_rollout_failures VALUES (?,'release_binary_unavailable')", (child_id,))

            # Advance worker to observe failed child and finalize parent
            service.run_one()
            service.run_one()
            self.assertEqual(service.get(self.actor, job['id'])['status'], 'blocked')

            # Recovery via resume
            resumed = service.resume(self.actor, job['id'])
            self.assertEqual(resumed['status'], 'running')
            child = AgentRolloutService(self.db).get(self.actor, child_id)
            self.assertEqual(child['status'], 'awaiting_executor')

            # Child succeeds on next attempt and parent finishes succeeded
            with self.db.transaction() as conn:
                conn.execute("UPDATE backend_agent_rollouts SET status='succeeded' WHERE id=?", (child_id,))
            service.driver.inspect_node_services.return_value = {'agent_commit': 'a' * 40, 'runtime_commit': 'a' * 40}
            service.run_one()
            service.run_one()
            self.assertEqual(service.get(self.actor, job['id'])['status'], 'succeeded')

            # Next operation: queue runtimes update and dispatch
            service.driver.inspect_node_services.return_value = {'agent_commit': 'a' * 40, 'runtime_commit': 'old' * 10}
            next_job = service.queue(self.actor, str(uuid4()), 'runtimes')
            self.assertEqual(next_job['status'], 'awaiting_executor')
            service.run_one()
            self.assertEqual(service.get(self.actor, next_job['id'])['status'], 'running')
