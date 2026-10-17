"""Coordinator recovery must observe existing commands, never replay them."""
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5
import unittest

from backend.authorization import AccessDenied
from backend.node_bootstrap import NodeBootstrapService
from backend.node_operations import NodeOperations
from backend.nodes import NodeService
from tests import test_backend_node_jobs as fixtures


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        fixtures.BackendNodeJobTests.setUp(self)
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_nodes SET applied_revision=0,enabled=0 WHERE key='n1'")
        self.service = NodeBootstrapService(self.db, SimpleNamespace(
            inspect_node_services=lambda key: {'docker': True}))

    def queue(self):
        return self.service.queue(self.actor, 'n1', 1, str(uuid4()))

    def test_explicit_protocol_recovery_fences_old_child_and_keeps_parent(self):
        from backend.recovery import act
        from unittest.mock import Mock
        import json
        parent = self.queue()
        for _ in range(3):
            self.service.run_one()
        child = self.service.get(self.actor, parent['id'])['child_id']
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_node_jobs SET status='blocked' WHERE id=?", (child,))
        self.service.run_one()
        driver = SimpleNamespace(node_action=Mock(return_value={
            'node_key': 'n1', 'action': 'bootstrap', 'revision': 2, 'retired': True}))
        result = act(self.db, self.actor, driver, 'bootstrap', parent['id'], 'resolve')
        self.assertEqual(result['status'], 'running')
        current = self.service.get(self.actor, parent['id'])
        self.assertNotEqual(current['child_id'], child)
        with self.db.connect() as conn:
            old = conn.execute('SELECT status FROM backend_node_jobs WHERE id=?', (child,)).fetchone()
            new = conn.execute('SELECT * FROM backend_node_jobs WHERE id=?', (current['child_id'],)).fetchone()
        self.assertEqual(old['status'], 'superseded')
        self.assertEqual(new['status'], 'awaiting_executor')
        self.assertEqual(json.loads(new['intent_json'])['command_id'], new['id'])
        driver.node_action.assert_called_once()
        self.assertEqual(driver.node_action.call_args.args[1], 'resolve_bootstrap')

    def test_protocol_setup_cancellation_fences_child_without_reinstalling(self):
        from backend.recovery import act
        from backend.node_lifecycle import NodeLifecycle
        from unittest.mock import Mock
        parent=self.queue()
        for _ in range(3): self.service.run_one()
        child=self.service.get(self.actor,parent['id'])['child_id']
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_node_jobs SET status='blocked' WHERE id=?",(child,))
        self.service.run_one()
        driver=SimpleNamespace(node_action=Mock(return_value={'node_key':'n1','action':'bootstrap','revision':2,'retired':True}))
        result=act(self.db,self.actor,driver,'bootstrap',parent['id'],'cancel')
        self.assertEqual(result['status'],'superseded')
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) AS n FROM backend_node_jobs').fetchone()['n'],1)
        # Setup no longer pins the node; normal drain/removal can proceed.
        self.assertEqual(NodeLifecycle(self.db).start_drain(self.actor,'n1')['status'],'draining')

    def test_protocol_recovery_without_retirement_evidence_does_not_dispatch(self):
        from backend.recovery import act
        from unittest.mock import Mock
        parent = self.queue()
        for _ in range(3): self.service.run_one()
        child = self.service.get(self.actor,parent['id'])['child_id']
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_node_jobs SET status='blocked' WHERE id=?", (child,))
        self.service.run_one()
        driver=SimpleNamespace(node_action=Mock(side_effect=TimeoutError('uncertain')))
        with self.assertRaises(AccessDenied):
            act(self.db,self.actor,driver,'bootstrap',parent['id'],'resolve')
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) AS n FROM backend_node_jobs').fetchone()['n'],1)
        self.assertEqual(self.service.get(self.actor,parent['id'])['status'],'blocked')

    def test_ssh_preflight_failure_can_resume_or_cancel_without_fake_success(self):
        from backend.agent_rollout import AgentRolloutService
        from backend.installation_progress import progress_path, write_progress
        from backend.recovery import act, overview
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        import os
        parent = self.queue()
        # A child exists but failed before any remote installation command.
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_node_connections(node_key,transport,ssh_target) VALUES ('n1','ssh','root@n1.example') ON CONFLICT(node_key) DO UPDATE SET transport='ssh',ssh_target='root@n1.example'")
        child = AgentRolloutService(self.db).request(self.actor, 'n1', str(uuid4()),
            transport='ssh', ssh_target='root@n1.example', bootstrap_id=parent['id'])
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_agent_rollouts SET status='blocked' WHERE id=?", (child['id'],))
            conn.execute('INSERT INTO backend_agent_rollout_failures VALUES (?,?)', (child['id'], 'ssh_authentication'))
            conn.execute("UPDATE backend_node_bootstraps SET status='blocked',child_id=?,child_kind='agent-rollouts',error_code='child_operation_blocked' WHERE id=?", (child['id'], parent['id']))
        with TemporaryDirectory() as shared, patch.dict(os.environ, NODE_PLANE_SHARED_DIR=shared):
            write_progress(progress_path(child['id']), child['id'], 'check SSH prerequisites for n1')
            self.assertIn('resume', next(x for x in overview(self.db,self.actor)['items'] if x['kind']=='agent')['actions'])
            result = act(self.db,self.actor,None,'bootstrap',parent['id'],'resolve')
            self.assertEqual(result['status'], 'running')
            self.assertEqual(AgentRolloutService(self.db).get(self.actor,child['id'])['status'], 'awaiting_executor')
            with self.db.transaction() as conn:
                conn.execute("UPDATE backend_agent_rollouts SET status='blocked' WHERE id=?", (child['id'],))
                conn.execute('INSERT INTO backend_agent_rollout_failures VALUES (?,?)', (child['id'], 'ssh_authentication'))
                conn.execute("UPDATE backend_node_bootstraps SET status='blocked' WHERE id=?", (parent['id'],))
            result = act(self.db,self.actor,None,'bootstrap',parent['id'],'cancel')
            self.assertEqual(result['status'], 'superseded')
            self.assertEqual(overview(self.db,self.actor)['items'], [])
            # Cancellation stays terminal in history, never succeeded.
            self.assertEqual(AgentRolloutService(self.db).get(self.actor,child['id'])['status'], 'cancelled')

    def test_overview_can_read_active_bootstrap_and_recoverable_agent_wait(self):
        from backend.node_overview import NodeOverviewService
        parent = self.queue()
        overview = NodeOverviewService(self.db).get(self.actor, 'n1')
        self.assertEqual(overview['bootstrap']['id'], parent['id'])
        self.assertEqual(overview['state'], 'applying')
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_node_bootstraps SET status='blocked',error_code='node_agent_unavailable',phase='agent' WHERE id=?", (parent['id'],))
        overview = NodeOverviewService(self.db).get(self.actor, 'n1')
        self.assertEqual(overview['state'], 'applying')
        self.assertTrue(overview['bootstrap']['waiting_for_agent'])

    def test_existing_agent_and_docker_are_observed_before_protocols(self):
        parent = self.queue()
        self.assertTrue(self.service.run_one())
        self.assertEqual(self.service.get(self.actor, parent['id'])['phase'], 'docker')
        self.assertTrue(self.service.run_one())
        self.assertEqual(self.service.get(self.actor, parent['id'])['phase'], 'protocols')
        self.assertTrue(self.service.run_one())
        child = self.service.get(self.actor, parent['id'])['child_id']
        self.assertFalse(self.service.run_one())
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_node_jobs SET status='succeeded' WHERE id=?", (child,))
        self.assertTrue(self.service.run_one())
        self.assertEqual(self.service.get(self.actor, parent['id'])['status'], 'succeeded')

    def test_crash_after_protocol_queue_adopts_original_command_despite_revision_change(self):
        parent = self.queue()
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_node_bootstraps SET phase='protocols' WHERE id=?", (parent['id'],))
        child = NodeOperations(self.db).queue(self.actor, 'n1', 'bootstrap', revision=1,
            command_key=str(uuid5(UUID(parent['id']), 'protocols')), bootstrap_id=parent['id'])
        self.assertEqual(NodeService(self.db).get(self.actor, 'n1')['desired_revision'], 2)
        self.assertTrue(self.service.run_one())
        recovered = self.service.get(self.actor, parent['id'])
        self.assertEqual(recovered['child_id'], child['id'])
        self.assertEqual(recovered['status'], 'running')
        self.assertFalse(self.service.run_one())
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM backend_node_jobs').fetchone()[0], 1)

    def test_blocked_child_is_not_replayed_and_confirmed_recovery_advances(self):
        parent = self.queue()
        for _ in range(3):
            self.service.run_one()
        child = self.service.get(self.actor, parent['id'])['child_id']
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_node_jobs SET status='blocked' WHERE id=?", (child,))
        self.assertTrue(self.service.run_one())
        self.assertEqual(self.service.get(self.actor, parent['id'])['status'], 'blocked')
        self.assertFalse(self.service.run_one())
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_node_jobs SET status='succeeded' WHERE id=?", (child,))
        self.assertTrue(self.service.run_one())
        self.assertEqual(self.service.get(self.actor, parent['id'])['status'], 'succeeded')

    def test_pending_parent_blocks_metadata_changes_and_other_jobs(self):
        self.queue()
        with self.assertRaises(AccessDenied):
            NodeService(self.db).command(self.actor, action='edit', node_key='n1', revision=1,
                command_key=str(uuid4()), values={'title': 'Changed'})
        with self.assertRaises(AccessDenied):
            NodeOperations(self.db).queue(self.actor, 'n1', 'install_docker', revision=1,
                                         command_key=str(uuid4()))

    def test_restore_reconnect_installs_unreachable_agent_using_saved_connection(self):
        from unittest.mock import patch
        from backend.agent_rollout import AgentRolloutService
        def unavailable(key):
            raise TimeoutError()
        self.service.driver = SimpleNamespace(inspect_node_services=unavailable)
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_node_connections(node_key,transport,ssh_target) VALUES ('n1','local',NULL) ON CONFLICT(node_key) DO NOTHING")
        parent = self.service.queue(self.actor, 'n1', 1, str(uuid4()), recover_unreachable=True)
        with patch.object(AgentRolloutService, 'request', return_value={'id': 'restore-agent'}) as deploy:
            self.assertTrue(self.service.run_one())
        deploy.assert_called_once()
        self.assertEqual(deploy.call_args.kwargs['bootstrap_id'], parent['id'])
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT child_id FROM backend_node_bootstraps WHERE id=?', (parent['id'],)).fetchone()['child_id'], 'restore-agent')

    def test_unavailable_agent_probe_does_not_queue_reinstall(self):
        def unavailable(key):
            raise TimeoutError()
        self.service.driver = SimpleNamespace(inspect_node_services=unavailable)
        parent = self.queue()
        self.assertTrue(self.service.run_one())
        self.assertTrue(self.service.get(self.actor, parent['id'])['waiting_for_agent'])
        self.assertIsNone(self.service.get(self.actor, parent['id'])['error_code'])
        self.assertFalse(self.service.run_one())
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM backend_agent_rollouts').fetchone()[0], 0)

    def test_docker_probe_recovers_when_agent_becomes_reachable_without_reinstall(self):
        parent = self.queue()
        self.service.run_one()
        self.service.driver = SimpleNamespace(inspect_node_services=lambda key: (_ for _ in ()).throw(TimeoutError()))
        self.assertTrue(self.service.run_one())
        blocked = self.service.get(self.actor, parent['id'])
        self.assertEqual((blocked['phase'], blocked['status'], blocked['child_id']), ('docker', 'awaiting_executor', None))
        self.assertTrue(blocked['waiting_for_agent'])
        self.assertFalse(self.service.run_one())
        self.service.driver = SimpleNamespace(inspect_node_services=lambda key: {'docker': True})
        self.assertTrue(self.service.run_one())
        self.assertEqual(self.service.get(self.actor, parent['id'])['status'], 'awaiting_executor')
        self.assertTrue(self.service.run_one())
        self.assertEqual(self.service.get(self.actor, parent['id'])['phase'], 'protocols')
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM backend_agent_rollouts').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM backend_node_jobs').fetchone()[0], 0)

    def test_idempotency_retains_parent_and_confirmed_components_are_skipped(self):
        key = str(uuid4())
        first = self.service.queue(self.actor, 'n1', 1, key)
        self.assertEqual(first, self.service.queue(self.actor, 'n1', 1, key))
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_nodes SET applied_revision=1 WHERE key='n1'")
        self.service.driver = SimpleNamespace(inspect_node_services=lambda key: {
            'docker': True, 'awg_config_valid': True, 'awg_running': True,
            'xray_config_valid': True, 'xray_running': True})
        for _ in range(3):
            self.assertTrue(self.service.run_one())
        self.assertEqual(self.service.get(self.actor, first['id'])['status'], 'succeeded')
        with self.db.connect() as conn:
            self.assertFalse(conn.execute('SELECT 1 FROM backend_node_jobs').fetchone())
            self.assertFalse(conn.execute('SELECT 1 FROM backend_agent_rollouts').fetchone())

    def test_identity_mismatch_cannot_be_treated_as_missing_agent(self):
        import grpc
        class Mismatch(grpc.RpcError):
            def code(self):
                return grpc.StatusCode.FAILED_PRECONDITION
            def details(self):
                return 'agent node identity mismatch'
        def inspect(key):
            raise Mismatch()
        self.service.driver = SimpleNamespace(inspect_node_services=inspect)
        parent = self.queue()
        self.assertTrue(self.service.run_one())
        self.assertTrue(self.service.get(self.actor, parent['id'])['waiting_for_agent'])
        self.assertIsNone(self.service.get(self.actor, parent['id'])['error_code'])
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM backend_agent_rollouts').fetchone()[0], 0)

    def test_confirmed_missing_agent_queues_one_rollout_and_waits_for_confirmation(self):
        import grpc
        class Missing(grpc.RpcError):
            def code(self):
                return grpc.StatusCode.FAILED_PRECONDITION
            def details(self):
                return 'no node-agent target configured'
        def inspect(key):
            raise Missing()
        self.service.driver = SimpleNamespace(inspect_node_services=inspect)
        parent = self.queue()
        self.assertTrue(self.service.run_one())
        pending = self.service.get(self.actor, parent['id'])
        self.assertEqual(pending['child_kind'], 'agent-rollouts')
        self.assertFalse(self.service.run_one())
        from backend.installation_progress import progress_path, write_progress
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        import os
        with TemporaryDirectory() as shared, patch.dict(os.environ, NODE_PLANE_SHARED_DIR=shared):
            with self.db.transaction() as conn:
                conn.execute("UPDATE backend_agent_rollouts SET status='running' WHERE id=?", (pending['child_id'],))
            write_progress(progress_path(pending['child_id']), pending['child_id'], 'install node-agent on n1')
            observed = self.service.get(self.actor, parent['id'])
            self.assertEqual(observed['progress']['stage'], 'install_agent')
            self.assertEqual(observed['status'], 'running')
            self.assertFalse(self.service.run_one())
            with self.db.transaction() as conn:
                conn.execute("UPDATE backend_agent_rollouts SET status='blocked' WHERE id=?", (pending['child_id'],))
            self.assertTrue(self.service.run_one())
            self.assertEqual(self.service.get(self.actor, parent['id'])['progress']['stage'], 'install_agent')
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_agent_rollouts SET status='succeeded' WHERE id=?", (pending['child_id'],))
        self.assertTrue(self.service.run_one())
        self.assertEqual(self.service.get(self.actor, parent['id'])['phase'], 'docker')

    def test_resume_before_dispatch_keeps_parent_identity_and_queues_no_child(self):
        from backend.recovery import act, overview
        parent = self.queue()
        self.db.connection.execute("UPDATE backend_node_bootstraps SET status='blocked',error_code='node_agent_unavailable'")
        self.assertEqual(overview(self.db,self.actor)['items'][0]['actions'], ['recheck','resolve'])
        result = act(self.db,self.actor,self.service.driver,'bootstrap',parent['id'],'resolve')
        self.assertEqual((result['id'],result['status']), (parent['id'],'awaiting_executor'))
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_agent_rollouts').fetchone()[0],0)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_node_jobs').fetchone()[0],0)

    def test_resume_adopts_hidden_child_even_after_revision_advanced_and_never_replays(self):
        from backend.recovery import act
        parent = self.queue()
        self.db.connection.execute("UPDATE backend_node_bootstraps SET phase='protocols' WHERE id=?", (parent['id'],))
        child = NodeOperations(self.db).queue(self.actor,'n1','bootstrap',revision=1,
            command_key=str(uuid5(UUID(parent['id']),'protocols')),bootstrap_id=parent['id'])
        self.db.connection.execute("UPDATE backend_node_jobs SET status='blocked' WHERE id=?", (child['id'],))
        self.db.connection.execute("UPDATE backend_node_bootstraps SET status='blocked' WHERE id=?", (parent['id'],))
        result = act(self.db,self.actor,self.service.driver,'bootstrap',parent['id'],'resolve')
        self.assertEqual(result['status'],'running')
        self.assertEqual(self.service.get(self.actor,parent['id'])['child_id'],child['id'])
        self.assertTrue(self.service.run_one())
        self.assertFalse(self.service.run_one())
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_node_jobs').fetchone()[0],1)
