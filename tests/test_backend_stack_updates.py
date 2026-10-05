"""Controller update fences agent changes; node failures are partial results."""
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from tests import test_backend_updates as fixture
from backend.agent_rollout import AgentRolloutService


class StackUpdateTests(TestCase):
    setUp = fixture.BackendUpdateTests.setUp
    service = fixture.BackendUpdateTests.service
    node = fixture.BackendUpdateTests.node

    def queue(self, service):
        return service.queue(self.actor, str(uuid4()), 'stack',
            target_ref='v0.4.3-alpha.19', branch='dev')

    def test_core_update_blocks_mutations_until_verified(self):
        from backend.maintenance_gate import admit
        from backend.authorization import AccessDenied
        service = self.service()
        self.queue(service)
        with self.assertRaises(AccessDenied):
            with self.db.transaction() as conn:
                admit(conn)
        service.run_one()
        with patch.object(service, '_stack_progress', return_value={'status': 'succeeded'}):
            service.run_one()
        with self.db.transaction() as conn:
            admit(conn)

    def test_controller_failure_rolls_back_without_touching_agents(self):
        service = self.service()
        self.node()
        job = self.queue(service)
        service.run_one()
        service.updater.schedule_update.assert_called_once_with(branch='dev',
            target_ref='v0.4.3-alpha.19', stack_job_id=job['id'])
        service.updater.refresh_update_run_state.return_value['last_run_status'] = 'failed'
        with patch.object(service, '_stack_progress', return_value={'rollback_status': 'succeeded',
                'components': {'driver': 'failed'}}):
            service.run_one()
        result = service.get(self.actor, job['id'])
        self.assertEqual(result['status'], 'rolled_back')
        self.assertEqual(result['items'][0]['status'], 'skipped')
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_agent_rollouts').fetchone()[0], 0)

    def test_failed_rollback_is_not_reported_as_successful(self):
        service = self.service()
        job = self.queue(service)
        service.run_one()
        service.updater.refresh_update_run_state.return_value['last_run_status'] = 'failed'
        with patch.object(service, '_stack_progress', return_value={'rollback_status': 'failed'}):
            service.run_one()
        self.assertEqual(service.get(self.actor, job['id'])['status'], 'blocked')

    def test_agent_failure_is_partial_and_does_not_reinstall_controller(self):
        service = self.service()
        self.node()
        job = self.queue(service)
        service.run_one()
        with patch.object(service, '_stack_progress', return_value={'status': 'succeeded',
                'components': {k: 'succeeded' for k in ('backend', 'worker', 'driver', 'telegram')}}):
            service.run_one()
        service.run_one()
        runner = Mock(return_value=False)
        rollout = AgentRolloutService(self.db, runner)
        rollout.run_one()
        self.assertIn('--skip-driver', runner.call_args.args[0])
        service.run_one()
        service.run_one()
        result = service.get(self.actor, job['id'])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['items'][0]['title'], 'Latvia')
        self.assertEqual(result['items'][0]['region'], 'EU')
        service.updater.schedule_update.assert_called_once()

    def test_controller_completion_requires_durable_health_confirmation(self):
        service = self.service()
        self.node()
        job = self.queue(service)
        service.run_one()
        with patch.object(service, '_stack_progress', return_value={}):
            service.run_one()
        self.assertEqual(service.get(self.actor, job['id'])['status'], 'blocked')
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_agent_rollouts').fetchone()[0], 0)

    def test_agent_then_runtime_are_separate_durable_steps(self):
        service = self.service()
        self.node()
        self.db.connection.execute("UPDATE backend_nodes SET applied_revision=1 WHERE key='lv1'")
        self.db.connection.commit()
        job = self.queue(service)
        service.run_one()
        with patch.object(service, '_stack_progress', return_value={'status': 'succeeded'}):
            service.run_one()
        service.run_one()
        AgentRolloutService(self.db, lambda args: True).run_one()
        service.run_one()
        result = service.get(self.actor, job['id'])
        self.assertEqual(result['items'][0]['phase'], 'runtime')
        self.assertEqual(result['items'][0]['status'], 'awaiting_executor')
        service.run_one()
        self.db.connection.execute("UPDATE backend_node_jobs SET status='succeeded'")
        self.db.connection.commit()
        service.run_one()
        service.run_one()
        self.assertEqual(service.get(self.actor, job['id'])['status'], 'succeeded')
