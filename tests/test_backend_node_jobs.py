import json
from types import SimpleNamespace
import unittest
from uuid import uuid4

from tests.test_backend_identity import Database
from backend.identity_repository import SQLIdentityRepository
from backend.authorization import Actor, Account, Principal, PrincipalKind, ADMIN_PERMISSIONS, AccessDenied
from backend.profiles import ProfileRepository
from backend.profile_commands import ProfileCommands
from backend.nodes import NodeService
from backend.node_settings import NodeSettingsService
from backend.config_issuance import ConfigIssuanceService
from backend.agent_rollout import AgentRolloutService
from backend.node_operations import NodeOperations


class BackendNodeJobTests(unittest.TestCase):
    def test_bootstrap_records_selected_auto_port_before_reconciling_profiles(self):
        from backend.node_settings import _digest
        with self.db.transaction() as conn:
            settings = dict(self.settings, awg_port=443, awg_port_mode='auto', awg_i1_preset='quic')
            conn.execute("UPDATE backend_nodes SET settings_json=? WHERE key='n1'", (json.dumps(settings),))
        job = self.queue('bootstrap')

        class Driver:
            def node_action(self, task_id, action, intent):
                effective = dict(intent, settings=dict(intent['settings'], awg_port=8443))
                return {'node_key': 'n1', 'action': action, 'revision': intent['revision'],
                    'result': {'node_key': 'n1', 'revision': intent['revision'], 'awg_port': 8443,
                        'settings_sha256': _digest(effective)}}

        self.assertTrue(NodeOperations(self.db, Driver()).run_one())
        with self.db.connect() as conn:
            row = conn.execute('SELECT status,intent_json FROM backend_node_jobs WHERE id=?', (job['id'],)).fetchone()
            self.assertEqual(row['status'], 'succeeded')
            self.assertEqual(json.loads(row['intent_json'])['settings']['awg_port'], 443)
            node = conn.execute("SELECT settings_json,desired_revision,applied_revision FROM backend_nodes WHERE key='n1'").fetchone()
            self.assertEqual(json.loads(node['settings_json'])['awg_port'], 8443)
            self.assertEqual(node['desired_revision'], node['applied_revision'])

    def test_removal_status_overrides_installation_and_settings_state(self):
        from backend.node_removal import NodeRemovalService
        from backend.node_overview import NodeOverviewService
        NodeRemovalService(self.db).request(self.actor, 'n1')
        for status, expected in [('queued', 'deleting'), ('running', 'deleting'),
                                 ('blocked', 'deletion_blocked')]:
            self.db.connection.execute('UPDATE backend_node_removals SET status=? WHERE node_key=?', (status, 'n1'))
            self.db.connection.commit()
            overview = NodeOverviewService(self.db).get(self.actor, 'n1')
            self.assertEqual(overview['state'], expected)
            self.assertEqual(overview['removal_status'], status)

    def setUp(self):
        self.db = Database()
        self.addCleanup(self.db.connection.close)
        identities = SQLIdentityRepository(self.db)
        identities.initialize_schema()
        account = identities.resolve_telegram(123)
        self.db.connection.execute("UPDATE backend_accounts SET role='admin', status='approved' WHERE id=?", (account.id,))
        self.actor = Actor(Principal('test', PrincipalKind.ACCOUNT, ADMIN_PERMISSIONS), Account(account.id, 'admin', 'approved'))
        ProfileRepository(self.db).initialize_schema()
        ProfileCommands(self.db).initialize_schema()
        NodeService(self.db).initialize_schema()
        NodeSettingsService(self.db).initialize_schema()
        ConfigIssuanceService(self.db).initialize_schema()
        AgentRolloutService(self.db).initialize_schema()
        self.settings = {'public_host': 'node.example', 'xray_sni': 'www.cloudflare.com',
            'xray_tcp_port': 443, 'xray_xhttp_port': 8443, 'xray_xhttp_path': '/assets', 'awg_port': 51820}
        NodeService(self.db).command(self.actor, action='create', command_key=str(uuid4()), values={
            'key': 'n1', 'title': 'Node', 'region': 'EU', 'transport': 'local',
            'protocols': ['awg', 'xray'], 'xray_transports': ['tcp', 'xhttp'], 'settings': self.settings})
        self.db.connection.execute('UPDATE backend_nodes SET enabled=1, applied_revision=1 WHERE key=?', ('n1',))
        self.jobs = NodeOperations(self.db)

    def queue(self, action='reinstall_clean', key=None):
        return self.jobs.queue(self.actor, 'n1', action, revision=1, command_key=key or str(uuid4()))

    def test_unstarted_initial_settings_task_does_not_block_bootstrap_forever(self):
        from backend.node_settings import NodeSettingsExecutor
        from backend.node_overview import NodeOverviewService
        self.db.connection.execute("UPDATE backend_nodes SET applied_revision=0,enabled=0 WHERE key='n1'")
        self.db.connection.commit()
        task = NodeSettingsService(self.db).queue(self.actor, 'n1', revision=1, command_key=str(uuid4()))
        class Driver:
            def inspect_node(self, node_key):
                return {'health_state': 'degraded', 'xray_config_present': False, 'awg_config_present': False}
            def prepare_node(self, node_key):
                raise AssertionError('Initial installation must use Bootstrap')
            def apply_node_settings(self, command_id, intent):
                raise AssertionError('No settings mutation may start')
        self.assertEqual(NodeOverviewService(self.db).get(self.actor, 'n1')['state'], 'not_installed')
        self.assertTrue(NodeSettingsExecutor(self.db, Driver()).run_one())
        self.assertEqual(NodeSettingsService(self.db).get(self.actor, task['id'])['error_code'], 'node_installation_required')
        self.assertEqual(self.queue('bootstrap')['status'], 'awaiting_executor')

    def test_reinstall_fences_config_issuance_and_reprovisions_with_new_revision(self):
        profile = ProfileRepository(self.db).create_profile(runtime_name='alice', display_name='Alice', owner_account_id=self.actor.account.id)
        self.db.connection.execute('INSERT INTO backend_grants VALUES (?, ?, ?)', (profile, 'n1', 'awg'))
        key = str(uuid4())
        job = self.queue(key=key)
        self.assertEqual(self.queue(key=key), job)
        self.assertEqual(NodeService(self.db).get(self.actor, 'n1')['desired_revision'], 2)
        self.assertFalse(NodeService(self.db).get(self.actor, 'n1')['enabled'])
        with self.assertRaises(AccessDenied):
            ConfigIssuanceService(self.db)._current(self.db.connection, profile, 'n1', 'awg', 'vpn')
        calls = []
        def execute(task_id, action, intent, **kwargs):
            calls.append(task_id)
            return {'node_key': 'n1', 'action': action, 'revision': intent['revision'], 'result': {'verified': True}}
        worker = NodeOperations(self.db, SimpleNamespace(node_action=execute))
        self.assertTrue(worker.run_one())
        self.assertFalse(worker.run_one())
        self.assertEqual(calls, [job['id']])
        self.assertEqual(worker.get(self.actor, job['id'])['status'], 'succeeded')
        node = NodeService(self.db).get(self.actor, 'n1')
        self.assertEqual((node['desired_revision'], node['applied_revision'], node['enabled']), (2, 2, True))
        task = self.db.connection.execute('SELECT intent_json FROM backend_operation_tasks WHERE node_key=?', ('n1',)).fetchone()
        self.assertEqual(json.loads(task['intent_json'])['action'], 'ensure')
        self.assertEqual(json.loads(task['intent_json'])['desired_revision'], 2)

    def test_uncertain_outcome_is_recovered_without_reexecuting(self):
        job = self.queue()
        calls = []
        def execute(task_id, action, intent, recover=False):
            calls.append(recover)
            if not recover:
                raise TimeoutError()
            return {'node_key': 'n1', 'action': action, 'revision': intent['revision'], 'result': {'verified': True}}
        worker = NodeOperations(self.db, SimpleNamespace(node_action=execute))
        worker.run_one()
        self.assertEqual(worker.get(self.actor, job['id'])['status'], 'blocked')
        with self.assertRaises(AccessDenied):
            NodeService(self.db).command(self.actor, action='edit', node_key='n1', revision=2,
                command_key=str(uuid4()), values={'title': 'Changed'})
        worker.reconcile_completed()
        self.assertEqual(calls, [False, True])
        self.assertEqual(worker.get(self.actor, job['id'])['status'], 'succeeded')

    def test_wrong_acknowledgment_cannot_enable_node(self):
        job = self.queue()
        driver = SimpleNamespace(node_action=lambda *args: {'node_key': 'another', 'action': 'reinstall_clean', 'revision': 2, 'result': {}})
        worker = NodeOperations(self.db, driver)
        worker.run_one()
        self.assertEqual(worker.get(self.actor, job['id'])['status'], 'blocked')
        self.assertFalse(NodeService(self.db).get(self.actor, 'n1')['enabled'])

    def test_cleanup_keeps_inventory_and_agent_but_disables_configs(self):
        job = self.queue('cleanup_runtime')
        driver = SimpleNamespace(node_action=lambda task, action, intent: {
            'node_key': 'n1', 'action': action, 'revision': intent['revision'], 'result': {'cleaned': True}})
        NodeOperations(self.db, driver).run_one()
        self.assertEqual(self.jobs.get(self.actor, job['id'])['status'], 'succeeded')
        self.assertFalse(NodeService(self.db).get(self.actor, 'n1')['enabled'])
        self.assertEqual(NodeService(self.db).get(self.actor, 'n1')['transport'], 'local')
