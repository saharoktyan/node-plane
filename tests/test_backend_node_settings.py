from __future__ import annotations

import json
import unittest
from uuid import uuid4

from backend.node_settings import NodeSettingsExecutor, _digest
from backend.node_lifecycle import NodeLifecycle
from backend.authorization import Actor, Principal, PrincipalKind
from tests.test_backend_http import BackendHTTPTests


class BackendNodeSettingsTests(unittest.TestCase):
    setUp = BackendHTTPTests.setUp

    def admin_headers(self, **extra):
        return {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101', **extra}

    def create(self):
        return self.client.post('/api/v1/nodes', headers=self.admin_headers(**{'Idempotency-Key': str(uuid4())}),
            json={'key': 'n1', 'title': 'Node 1', 'region': 'EU', 'protocols': ['awg', 'xray'],
                  'xray_transports': ['tcp', 'xhttp'],
                  'settings': {'public_host': 'node.example', 'xray_sni': 'www.cloudflare.com',
                               'xray_tcp_port': 443, 'xray_xhttp_port': 8443,
                               'xray_xhttp_path': '/assets', 'awg_port': 51820}})

    def queue(self, revision=1, key=None):
        return self.client.post('/api/v1/nodes/n1/apply-settings',
            headers=self.admin_headers(**{'Idempotency-Key': key or str(uuid4()), 'If-Match': f'"{revision}"'}))

    def test_apply_confirms_exact_revision_and_enables_draft(self):
        self.assertEqual(self.create().status_code, 201)
        key = str(uuid4())
        queued = self.queue(key=key)
        self.assertEqual(queued.status_code, 202, queued.text)
        self.assertEqual(self.queue(key=key).json(), queued.json())
        task_id = queued.json()['id']
        self.assertEqual(self.queue().json()['error']['code'], 'node_settings_operation_pending')

        class Driver:
            def inspect_node(self, node_key):
                return {'health_state': 'running', 'xray_config_present': True, 'awg_config_present': True}

            def apply_node_settings(self, command_id, intent):
                assert command_id == task_id
                return json.dumps({'node_key': 'n1', 'revision': 1,
                                   'settings_sha256': _digest(intent)})

        worker = NodeSettingsExecutor(self.db, Driver())
        self.assertTrue(worker.run_one())
        self.assertFalse(worker.run_one())
        result = self.client.get(f'/api/v1/node-settings-operations/{task_id}', headers=self.admin_headers())
        self.assertEqual(result.json()['status'], 'succeeded')
        node = self.client.get('/api/v1/nodes/n1', headers=self.admin_headers()).json()
        self.assertTrue(node['enabled'])
        self.assertEqual((node['desired_revision'], node['applied_revision']), (1, 1))
        self.assertEqual(self.queue().json()['error']['code'], 'settings_already_applied')

    def test_uncertain_result_blocks_and_journal_recovery_confirms(self):
        self.assertEqual(self.create().status_code, 201)
        task_id = self.queue().json()['id']

        class Driver:
            def inspect_node(self, node_key):
                return {'health_state': 'running', 'xray_config_present': True, 'awg_config_present': True}

            def apply_node_settings(self, command_id, intent):
                raise TimeoutError('unknown remote result')

            def recover_node_settings(self, command_id, intent):
                return json.dumps({'node_key': intent['node_key'], 'revision': intent['revision'],
                                   'settings_sha256': _digest(intent)})

        worker = NodeSettingsExecutor(self.db, Driver())
        self.assertTrue(worker.run_one())
        self.assertEqual(self.client.get(f'/api/v1/node-settings-operations/{task_id}', headers=self.admin_headers()).json()['status'], 'blocked')
        self.assertEqual(self.queue().status_code, 409)
        self.assertEqual(worker.reconcile_completed(), 1)
        self.assertEqual(self.db.connection.execute('SELECT applied_revision FROM backend_nodes WHERE key = ?', ('n1',)).fetchone()[0], 1)

    def test_bad_result_cannot_advance_revision_and_edit_supersedes_queued(self):
        self.assertEqual(self.create().status_code, 201)
        task_id = self.queue().json()['id']
        edited = self.client.patch('/api/v1/nodes/n1', headers=self.admin_headers(**{
            'Idempotency-Key': str(uuid4()), 'If-Match': '"1"'}), json={'title': 'Node 2'})
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual(self.client.get(f'/api/v1/node-settings-operations/{task_id}', headers=self.admin_headers()).json()['status'], 'superseded')
        next_id = self.queue(revision=2).json()['id']

        class Driver:
            def inspect_node(self, node_key):
                return {'health_state': 'running', 'xray_config_present': True, 'awg_config_present': True}

            def apply_node_settings(self, command_id, intent):
                return json.dumps({'node_key': 'wrong', 'revision': 2,
                                   'settings_sha256': _digest(intent)})

        self.assertTrue(NodeSettingsExecutor(self.db, Driver()).run_one())
        self.assertEqual(self.client.get(f'/api/v1/node-settings-operations/{next_id}', headers=self.admin_headers()).json()['status'], 'blocked')
        self.assertEqual(self.db.connection.execute('SELECT applied_revision FROM backend_nodes WHERE key = ?', ('n1',)).fetchone()[0], 0)

    def test_absent_agent_does_not_turn_queued_apply_into_uncertain_result(self):
        self.assertEqual(self.create().status_code, 201)
        task_id = self.queue().json()['id']

        class Driver:
            def inspect_node(self, node_key):
                raise ConnectionError('agent is offline')

            def apply_node_settings(self, command_id, intent):
                raise AssertionError('mutation must not start')

        self.assertFalse(NodeSettingsExecutor(self.db, Driver()).run_one())
        self.assertEqual(self.client.get(f'/api/v1/node-settings-operations/{task_id}', headers=self.admin_headers()).json()['status'], 'awaiting_executor')

    def test_clean_agent_is_prepared_before_settings_command(self):
        self.assertEqual(self.create().status_code, 201)
        task_id = self.queue().json()['id']
        calls = []

        class Driver:
            def inspect_node(self, node_key):
                calls.append('inspect')
                return {'health_state': 'degraded', 'xray_config_present': False,
                        'awg_config_present': False}

            def prepare_node(self, node_key):
                calls.append('prepare')

            def apply_node_settings(self, command_id, intent):
                calls.append('apply')
                assert command_id == task_id
                return json.dumps({'node_key': 'n1', 'revision': 1,
                                   'settings_sha256': _digest(intent)})

        self.assertTrue(NodeSettingsExecutor(self.db, Driver()).run_one())
        self.assertEqual(calls, ['inspect', 'prepare', 'apply'])
        self.assertEqual(self.db.connection.execute('SELECT applied_revision FROM backend_nodes WHERE key = ?',
            ('n1',)).fetchone()['applied_revision'], 1)

    def test_failed_preparation_leaves_command_queued(self):
        self.assertEqual(self.create().status_code, 201)
        task_id = self.queue().json()['id']

        class Driver:
            def inspect_node(self, node_key):
                return {'health_state': 'degraded', 'xray_config_present': False,
                        'awg_config_present': False}

            def prepare_node(self, node_key):
                raise TimeoutError('Docker install still running')

            def apply_node_settings(self, command_id, intent):
                raise AssertionError('settings command must not start')

        self.assertFalse(NodeSettingsExecutor(self.db, Driver()).run_one())
        self.assertEqual(self.client.get(f'/api/v1/node-settings-operations/{task_id}',
            headers=self.admin_headers()).json()['status'], 'awaiting_executor')

    def test_blocked_repair_queues_fresh_revision_and_never_confirms_old_task(self):
        self.assertEqual(self.create().status_code, 201)
        old_id = self.queue().json()['id']
        self.db.connection.execute("UPDATE backend_node_settings_tasks SET status = 'blocked' WHERE id = ?", (old_id,))
        actor = Actor(Principal('maintenance', PrincipalKind.ACCOUNT,
                                frozenset({'maintenance.manage'}), self.admin.id), self.admin)

        class Driver:
            def resolve_node_settings(self, command_id, intent):
                assert command_id == old_id and intent['revision'] == 1
                return {'config_matches': False, 'containers_running': True}

            def inspect_node(self, node_key):
                return {'health_state': 'running', 'xray_config_present': True, 'awg_config_present': True}

            def apply_node_settings(self, command_id, intent):
                assert command_id != old_id and intent['revision'] == 2
                return json.dumps({'node_key': 'n1', 'revision': 2,
                                   'settings_sha256': _digest(intent)})

        worker = NodeSettingsExecutor(self.db, Driver())
        result = worker.resolve_blocked(actor, old_id)
        self.assertEqual(result['revision'], 2)
        self.assertEqual(self.db.connection.execute('SELECT status FROM backend_node_settings_tasks WHERE id = ?',
            (old_id,)).fetchone()['status'], 'superseded')
        self.assertEqual(self.db.connection.execute('SELECT applied_revision FROM backend_nodes WHERE key = ?',
            ('n1',)).fetchone()['applied_revision'], 0)
        self.assertTrue(worker.run_one())
        self.assertEqual(self.db.connection.execute('SELECT applied_revision FROM backend_nodes WHERE key = ?',
            ('n1',)).fetchone()['applied_revision'], 2)

    def test_incomplete_draft_and_non_admin_cannot_queue(self):
        response = self.client.post('/api/v1/nodes', headers=self.admin_headers(**{'Idempotency-Key': str(uuid4())}),
            json={'key': 'n1', 'title': 'N', 'region': 'EU', 'protocols': ['xray']})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.queue().json()['error']['code'], 'node_settings_incomplete')
        member = self.client.post('/api/v1/integrations/telegram/identities/resolve',
            headers={**self.headers, 'Idempotency-Key': str(uuid4())},
            json={'telegram_user_id': 102}).json()
        self.db.connection.execute("UPDATE backend_accounts SET status = 'approved' WHERE id = ?", (member['id'],))
        denied = self.client.post('/api/v1/nodes/n1/apply-settings', headers={**self.headers,
            'X-Node-Plane-Telegram-User-ID': '102', 'Idempotency-Key': str(uuid4()), 'If-Match': '"1"'})
        self.assertEqual(denied.status_code, 403)

    def test_unsafe_host_is_rejected_before_remote_command(self):
        response = self.client.post('/api/v1/nodes', headers=self.admin_headers(**{
            'Idempotency-Key': str(uuid4())}), json={
            'key': 'n1', 'title': 'N', 'region': 'EU', 'protocols': ['xray'],
            'settings': {'public_host': 'node.example', 'xray_sni': 'example.com";print(1)',
                         'xray_tcp_port': 443, 'xray_xhttp_port': 8443,
                         'xray_xhttp_path': '/assets'}})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.queue().json()['error']['code'], 'invalid_input')

    def test_registry_only_retirement_can_abandon_uncertain_settings(self):
        self.assertEqual(self.create().status_code, 201)
        task_id = self.queue().json()['id']
        self.db.connection.execute("UPDATE backend_node_settings_tasks SET status = 'blocked' WHERE id = ?", (task_id,))
        actor = Actor(Principal('maintenance', PrincipalKind.ACCOUNT,
                                frozenset({'maintenance.manage'}), self.admin.id), self.admin)
        lifecycle = NodeLifecycle(self.db)
        with self.assertRaises(Exception) as error:
            lifecycle.start_drain(actor, 'n1')
        self.assertEqual(getattr(error.exception, 'code', None), 'node_settings_uncertain')
        result = lifecycle.retire_registry_only(actor, 'n1', 'VPS is permanently unavailable for this test')
        self.assertEqual(result['mode'], 'registry_only')
        self.assertEqual(self.db.connection.execute('SELECT status FROM backend_node_settings_tasks WHERE id = ?',
            (task_id,)).fetchone()['status'], 'superseded')
