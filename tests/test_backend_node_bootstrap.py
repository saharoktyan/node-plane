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

    def test_unavailable_agent_probe_does_not_queue_reinstall(self):
        def unavailable(key):
            raise TimeoutError()
        self.service.driver = SimpleNamespace(inspect_node_services=unavailable)
        parent = self.queue()
        self.assertTrue(self.service.run_one())
        self.assertEqual(self.service.get(self.actor, parent['id'])['error_code'], 'node_agent_unavailable')
        self.assertFalse(self.service.run_one())
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM backend_agent_rollouts').fetchone()[0], 0)

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
        self.assertEqual(self.service.get(self.actor, parent['id'])['error_code'], 'node_agent_unavailable')
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
