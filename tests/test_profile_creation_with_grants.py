from __future__ import annotations

import json
import unittest
from uuid import uuid4

from backend.admin_cli import bootstrap_admin
from backend.authorization import Principal, PrincipalKind, resolve_actor
from backend.identity_repository import SQLIdentityRepository
from backend.nodes import NodeService
from backend.node_settings import NodeSettingsService
from backend.agent_rollout import AgentRolloutService
from backend.access_requests import AccessRequestService
from backend.admin_overview import AdminOverviewService
from backend.node_overview import NodeOverviewService
from backend.profile_commands import ProfileCommands
from backend.profiles import ProfileRepository, ProfileService
from tests.test_backend_identity import Database


class ProfileCreationWithGrantsTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.addCleanup(self.db.connection.close)
        self.identities = SQLIdentityRepository(self.db)
        self.identities.initialize_schema()
        ProfileRepository(self.db).initialize_schema()
        self.commands = ProfileCommands(self.db)
        self.commands.initialize_schema()
        NodeService(self.db).initialize_schema()
        NodeSettingsService(self.db).initialize_schema()
        AgentRolloutService(self.db).initialize_schema()
        AccessRequestService(self.db).initialize_schema()
        admin = bootstrap_admin(self.identities, 101)
        principal = Principal('test', PrincipalKind.ACCOUNT,
                              frozenset({'profiles.manage', 'profiles.self.read', 'nodes.manage'}), admin.id)
        self.actor = resolve_actor(principal, self.identities)
        self.db.connection.execute('''INSERT INTO backend_nodes
            (key, title, region, protocols_json) VALUES (?, ?, ?, ?)''',
            ('n1', 'Node 1', 'test', json.dumps(['awg', 'xray'])))

    def test_grants_and_provisioning_intent_are_committed_together(self):
        key = str(uuid4())
        values = {'display_name': 'Alice', 'grants': [
            {'node_key': 'n1', 'protocol': 'awg'}]}
        result = self.commands.execute(self.actor, key, action='create', values=values)
        self.assertEqual(result['grants'], values['grants'])
        self.assertEqual(result['runtime_status'], 'awaiting_executor')
        self.assertEqual(self.commands.execute(self.actor, key, action='create',
                         values=values), result)
        self.assertEqual(self.db.connection.execute(
            'SELECT COUNT(*) FROM backend_profiles').fetchone()[0], 1)
        self.assertEqual(self.db.connection.execute(
            'SELECT COUNT(*) FROM backend_operation_tasks').fetchone()[0], 1)

    def test_invalid_grant_does_not_create_profile(self):
        from backend.authorization import AccessDenied

        with self.assertRaises(AccessDenied):
            self.commands.execute(self.actor, str(uuid4()), action='create',
                values={'display_name': 'Alice', 'grants': [
                    {'node_key': 'missing', 'protocol': 'awg'}]})
        self.assertEqual(self.db.connection.execute(
            'SELECT COUNT(*) FROM backend_profiles').fetchone()[0], 0)

    def test_search_pages_across_the_entire_profile_table(self):
        repo = ProfileRepository(self.db)
        for index in range(230):
            repo.create_profile(runtime_name=f'user_{index}',
                display_name='Match' if index in (5, 210, 229) else 'Other')
        service = ProfileService(repo)
        first = service.list_all(self.actor, search='match', limit=2)
        self.assertEqual(len(first['items']), 2)
        self.assertIsNotNone(first['next_cursor'])
        second = service.list_all(self.actor, search='MATCH', limit=2,
                                  cursor=first['next_cursor'])
        self.assertEqual(len(second['items']), 1)
        self.assertIsNone(second['next_cursor'])

    def test_delete_closes_access_and_keeps_remote_cleanup_intent(self):
        created = self.commands.execute(self.actor, str(uuid4()), action='create',
            values={'display_name': 'Alice', 'owner_account_id': self.actor.account.id,
                    'grants': [{'node_key': 'n1', 'protocol': 'awg'}]})
        profile_id = created['profile']['id']
        key = str(uuid4())
        deleted = self.commands.execute(self.actor, key, action='delete',
            profile_id=profile_id, revision=1)
        self.assertTrue(deleted['profile']['deleting'])
        self.assertEqual(deleted['grants'], [])
        self.assertEqual(self.commands.execute(self.actor, key, action='delete',
            profile_id=profile_id, revision=1), deleted)
        from backend.authorization import AccessDenied
        with self.assertRaises(AccessDenied):
            self.commands.execute(self.actor, str(uuid4()), action='grants',
                profile_id=profile_id, revision=2,
                values={'grants': [{'node_key': 'n1', 'protocol': 'awg'}]})
        task = self.db.connection.execute('''SELECT action FROM backend_operation_tasks
            WHERE operation_id = ?''', (deleted['operation_id'],)).fetchone()
        self.assertEqual(task['action'], 'delete')
        service = ProfileService(ProfileRepository(self.db))
        self.assertEqual(service.list_owned(self.actor)['items'], [])
        self.assertTrue(service.list_all(self.actor)['items'][0]['deleting'])
        self.db.connection.execute('''UPDATE backend_operations SET status = 'succeeded'
            WHERE id = ?''', (deleted['operation_id'],))
        self.assertEqual(service.list_all(self.actor)['items'], [])

    def test_admin_overview_uses_database_state_and_blocked_tasks(self):
        created = self.commands.execute(self.actor, str(uuid4()), action='create',
            values={'display_name': 'Alice', 'grants': [
                {'node_key': 'n1', 'protocol': 'awg'}]})
        task_id = self.db.connection.execute('''SELECT id FROM backend_operation_tasks
            WHERE operation_id = ?''', (created['operation_id'],)).fetchone()['id']
        self.db.connection.execute("UPDATE backend_operation_tasks SET status = 'blocked' WHERE id = ?", (task_id,))
        overview = AdminOverviewService(self.db).get(self.actor)
        self.assertEqual(overview['nodes_enabled'], 1)
        self.assertEqual(overview['profiles_active'], 1)
        self.assertEqual(overview['problem_nodes'], [{'key': 'n1', 'title': 'Node 1', 'region': 'test', 'flag': ''}])

    def test_node_overview_distinguishes_saved_state_from_live_health(self):
        overview = NodeOverviewService(self.db)
        initial = overview.get(self.actor, 'n1')
        self.assertEqual(initial['state'], 'not_installed')
        self.assertEqual(initial['access_total'], 0)
        self.assertFalse(initial['settings_complete'])
        self.commands.execute(self.actor, str(uuid4()), action='create',
            values={'display_name': 'Alice', 'grants': [
                {'node_key': 'n1', 'protocol': 'awg'}]})
        queued = overview.get(self.actor, 'n1')
        self.assertEqual((queued['pending'], queued['ready']), (1, 0))
        self.db.connection.execute('''UPDATE backend_nodes SET applied_revision = desired_revision
            WHERE key = ?''', ('n1',))
        self.db.connection.execute('''UPDATE backend_operation_tasks SET status = 'succeeded'
            WHERE node_key = ?''', ('n1',))
        applied = overview.get(self.actor, 'n1')
        self.assertEqual(applied['state'], 'applied_unverified')
        self.assertEqual(applied['ready'], 1)
        self.db.connection.execute('UPDATE backend_nodes SET enabled = 0 WHERE key = ?', ('n1',))
        inactive = overview.get(self.actor, 'n1')
        self.assertEqual(inactive['state'], 'inactive')
        self.assertEqual((inactive['ready'], inactive['attention']), (0, 1))
        self.db.connection.execute('UPDATE backend_nodes SET enabled = 1 WHERE key = ?', ('n1',))
        self.db.connection.execute('''UPDATE backend_nodes SET desired_revision = desired_revision + 1
            WHERE key = ?''', ('n1',))
        drift = overview.get(self.actor, 'n1')
        self.assertEqual(drift['state'], 'changes_pending')
        self.assertEqual((drift['ready'], drift['attention']), (0, 1))
        self.db.connection.execute('UPDATE backend_nodes SET settings_json = ? WHERE key = ?',
            (json.dumps({'public_host': 'node.example.com', 'awg_port': 51820,
                'xray_sni': 'www.cloudflare.com', 'xray_tcp_port': 443,
                'xray_xhttp_port': 8443, 'xray_xhttp_path': '/assets'}), 'n1'))
        self.assertTrue(overview.get(self.actor, 'n1')['settings_complete'])

    def test_node_search_pages_beyond_the_first_hundred(self):
        for index in range(220):
            key = f'node_{index:03d}'
            self.db.connection.execute('''INSERT INTO backend_nodes
                (key, title, region, protocols_json) VALUES (?, ?, ?, ?)''',
                (key, 'Target' if index in (5, 205, 219) else 'Other',
                 'test', json.dumps(['awg'])))
        service = NodeService(self.db)
        first = service.list(self.actor, search='target', limit=2)
        self.assertEqual(len(first['items']), 2)
        self.assertIsNotNone(first['next_cursor'])
        second = service.list(self.actor, search='TARGET', limit=2,
                              cursor=first['next_cursor'])
        self.assertEqual(len(second['items']), 1)
        self.assertIsNone(second['next_cursor'])

    def test_node_connection_survives_reads_and_legacy_nodes_have_no_connection(self):
        service = NodeService(self.db)
        values = {'key': 'remote1', 'title': 'Remote 1', 'region': 'test',
                  'protocols': ['awg'], 'transport': 'ssh',
                  'ssh_target': 'root@remote.example.com'}
        created = service.command(self.actor, action='create', values=values,
                                  command_key=str(uuid4()))
        self.assertEqual(created['transport'], 'ssh')
        self.assertEqual(created['ssh_target'], 'root@remote.example.com')
        self.assertEqual(service.get(self.actor, 'remote1')['ssh_target'],
                         'root@remote.example.com')
        self.assertEqual(service.list(self.actor, search='remote1')['items'][0]['transport'], 'ssh')
        self.assertIsNone(service.get(self.actor, 'n1')['transport'])
        from backend.authorization import AccessDenied
        with self.assertRaises(AccessDenied) as mismatch:
            AgentRolloutService(self.db).request(self.actor, 'remote1', str(uuid4()),
                transport='local')
        self.assertEqual(mismatch.exception.code, 'node_connection_mismatch')
        local = service.command(self.actor, action='edit', node_key='remote1',
            revision=created['desired_revision'], command_key=str(uuid4()),
            values={'transport': 'local', 'ssh_target': None})
        self.assertEqual((local['transport'], local['ssh_target']), ('local', None))
        switched_back = service.command(self.actor, action='edit', node_key='remote1',
            revision=local['desired_revision'], command_key=str(uuid4()),
            values={'transport': 'ssh', 'ssh_target': 'admin@new.example.com'})
        self.assertEqual(switched_back['ssh_target'], 'admin@new.example.com')

    def test_node_connection_requires_target_for_ssh_and_none_for_local(self):
        from backend.authorization import AccessDenied
        service = NodeService(self.db)
        base = {'key': 'remote1', 'title': 'Remote 1', 'region': 'test',
                'protocols': ['awg']}
        for connection in ({'transport': 'ssh'},
                           {'transport': 'local', 'ssh_target': 'root@example.com'}):
            with self.assertRaises(AccessDenied):
                service.command(self.actor, action='create', values=base | connection,
                                command_key=str(uuid4()))
        self.assertIsNone(self.db.connection.execute(
            'SELECT key FROM backend_nodes WHERE key = ?', ('remote1',)).fetchone())
