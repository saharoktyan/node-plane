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
    provision = BackendHTTPTests.provision

    def test_unchanged_installed_agent_only_has_no_apply_and_rejects_empty_work(self):
        self.client.post('/api/v1/nodes', headers=self.admin_headers(**{'Idempotency-Key': str(uuid4())}),
            json={'key': 'n1', 'title': 'Agent only', 'region': 'EU', 'protocols': []})
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_agent_rollouts (id,node_key,actor_account_id,command_key,intent_json,status) VALUES (?,'n1',?,?,'{}','succeeded')",
                         (str(uuid4()), self.admin.id, str(uuid4())))
        for _ in range(2):
            node = self.client.get('/api/v1/nodes/n1', headers=self.admin_headers()).json()
            self.assertTrue(node['agent_installed'])
            self.assertFalse(node['settings_apply_pending'])
            self.assertEqual((node['desired_revision'], node['applied_revision']), (1, 0))
        self.assertEqual(self.queue().status_code, 409)
        with self.db.connect() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM backend_node_settings_tasks WHERE node_key='n1'").fetchone())

    def test_explicit_plan_survives_transport_without_granting_implicit_removal(self):
        from backend.driver_transport import GrpcIntentDriver
        intent = {'node_key': 'n1', 'revision': 2, 'protocols': ['awg'], 'settings': {},
                  'protocol_plan': {'remove_protocols': []}}
        request = GrpcIntentDriver.node_settings_request(intent)
        self.assertEqual(json.loads(request.protocols_json), {'protocols': ['awg'], 'protocol_plan': {'remove_protocols': []}})
        legacy = dict(intent)
        legacy.pop('protocol_plan')
        self.assertEqual(json.loads(GrpcIntentDriver.node_settings_request(legacy).protocols_json), ['awg'])

    def test_old_queued_empty_request_is_retired_before_any_agent_rpc(self):
        self.client.post('/api/v1/nodes', headers=self.admin_headers(**{'Idempotency-Key': str(uuid4())}),
            json={'key': 'n1', 'title': 'Agent only', 'region': 'EU', 'protocols': []})
        with self.db.transaction() as conn:
            from backend.node_settings import _snapshot
            intent = _snapshot(conn.execute("SELECT * FROM backend_nodes WHERE key='n1'").fetchone())
            task = str(uuid4())
            conn.execute("INSERT INTO backend_node_settings_tasks(id,node_key,actor_account_id,command_key,revision,intent_json,status) VALUES (?,'n1',?,?,1,?,'awaiting_executor')",
                         (task, self.admin.id, str(uuid4()), json.dumps(intent)))
        from unittest.mock import Mock
        driver = Mock()
        self.assertTrue(NodeSettingsExecutor(self.db, driver).run_one())
        self.assertEqual(driver.mock_calls, [])
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT status FROM backend_node_settings_tasks WHERE id=?', (task,)).fetchone()['status'], 'superseded')

    def test_resolving_old_empty_agent_only_command_fences_without_reapplying(self):
        self.client.post('/api/v1/nodes', headers=self.admin_headers(**{'Idempotency-Key': str(uuid4())}),
            json={'key': 'n1', 'title': 'Agent only', 'region': 'EU', 'protocols': []})
        with self.db.transaction() as conn:
            node = conn.execute("SELECT * FROM backend_nodes WHERE key='n1'").fetchone()
            from backend.node_settings import _snapshot
            intent = _snapshot(node)
            task = str(uuid4())
            conn.execute("INSERT INTO backend_node_settings_tasks(id,node_key,actor_account_id,command_key,revision,intent_json,status) VALUES (?,'n1',?,?,1,?,'blocked')",
                         (task, self.admin.id, str(uuid4()), json.dumps(intent)))
        from unittest.mock import Mock
        from backend.authorization import Actor, Principal, PrincipalKind, ADMIN_PERMISSIONS, Account
        actor = Actor(Principal('test', PrincipalKind.SERVICE, ADMIN_PERMISSIONS), Account(self.admin.id, 'admin', 'approved'))
        driver = Mock()
        driver.resolve_node_settings.return_value = {'config_matches': False, 'containers_running': False}
        result = NodeSettingsExecutor(self.db, driver).resolve_blocked(actor, task)
        self.assertTrue(result['settings_not_required'])
        self.assertEqual(result['task_id'], task)
        driver.resolve_node_settings.assert_called_once_with(task, intent)
        driver.apply_node_settings.assert_not_called()
        with self.db.connect() as conn:
            rows = conn.execute("SELECT id,status FROM backend_node_settings_tasks WHERE node_key='n1'").fetchall()
            self.assertEqual([(r['id'], r['status']) for r in rows], [(task, 'superseded')])

    def test_empty_settings_cannot_start_runtime_work_on_an_uninstalled_node(self):
        self.client.post('/api/v1/nodes',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4())}),
            json={'key': 'n1', 'title': 'Empty', 'region': 'EU', 'protocols': []})
        response = self.queue()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()['error']['code'], 'node_installation_required')
        with self.db.connect() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM backend_node_settings_tasks WHERE node_key='n1'").fetchone())

    def test_first_protocol_can_be_applied_to_an_installed_agent_only_node(self):
        response = self.client.post('/api/v1/nodes',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4())}),
            json={'key': 'n1', 'title': 'Agent only', 'region': 'EU', 'protocols': [],
                  'settings': {'public_host': 'node.example'}})
        self.assertEqual(response.status_code, 201, response.text)
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_agent_rollouts (id,node_key,actor_account_id,command_key,intent_json,status) VALUES (?,'n1',?,?,'{}','succeeded')",
                         (str(uuid4()), self.admin.id, str(uuid4())))
        response = self.client.patch('/api/v1/nodes/n1',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"1"'}),
            json={'protocols': ['awg']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.queue(revision=2).status_code, 202)
        from unittest.mock import Mock
        driver = Mock()
        driver.inspect_node.return_value = {'health_state': 'degraded', 'xray_config_present': False, 'awg_config_present': False}
        def applied(command, intent):
            self.assertEqual(intent['protocols'], ['awg'])
            return json.dumps({'node_key': 'n1', 'revision': 2, 'settings_sha256': _digest(intent)})
        driver.apply_node_settings.side_effect = applied
        self.assertTrue(NodeSettingsExecutor(self.db, driver).run_one())
        driver.prepare_node.assert_called_once_with('n1')
        driver.apply_node_settings.assert_called_once()

    def test_removing_all_protocols_applies_an_empty_intent_and_retains_installed_agent(self):
        self.create()
        from types import SimpleNamespace
        initial = SimpleNamespace(
            inspect_node=lambda key: {'health_state': 'running', 'xray_config_present': True, 'awg_config_present': True},
            apply_node_settings=lambda command, intent: json.dumps({'node_key': 'n1', 'revision': 1, 'settings_sha256': _digest(intent)}))
        self.assertEqual(self.queue().status_code, 202)
        self.assertTrue(NodeSettingsExecutor(self.db, initial).run_one())
        response = self.client.patch('/api/v1/nodes/n1',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"1"'}),
            json={'protocols': [], 'xray_transports': []})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['agent_installed'])
        self.assertEqual(self.queue(revision=2).status_code, 202)
        from unittest.mock import Mock
        driver = Mock()
        driver.inspect_node.return_value = {'health_state': 'missing', 'xray_config_present': False, 'awg_config_present': False}
        def applied(command, intent):
            self.assertEqual(intent['protocols'], [])
            return json.dumps({'node_key': 'n1', 'revision': 2, 'settings_sha256': _digest(intent)})
        driver.apply_node_settings.side_effect = applied
        self.assertTrue(NodeSettingsExecutor(self.db, driver).run_one())
        driver.prepare_node.assert_not_called()
        node = self.client.get('/api/v1/nodes/n1', headers=self.admin_headers()).json()
        self.assertEqual(node['applied_revision'], 2)
        self.assertEqual(node['protocols'], [])
        self.assertTrue(node['agent_installed'])

        # Re-enable a protocol on the same managed runtime and journal, without
        # attempting PrepareNode (which intentionally rejects managed journals).
        response = self.client.patch('/api/v1/nodes/n1',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"2"'}),
            json={'protocols': ['awg']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.queue(revision=3).status_code, 202)
        def reapplied(command, intent):
            self.assertEqual(intent['protocols'], ['awg'])
            return json.dumps({'node_key': 'n1', 'revision': 3, 'settings_sha256': _digest(intent)})
        driver.apply_node_settings.side_effect = reapplied
        self.assertTrue(NodeSettingsExecutor(self.db, driver).run_one())
        driver.prepare_node.assert_not_called()

    def test_overview_exposes_pending_operation_and_reads_its_verified_progress(self):
        import os
        import tempfile
        from unittest.mock import patch
        from backend.installation_progress import progress_path, write_stage
        self.create()
        queued = self.queue().json()
        headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101'}
        overview = self.client.get('/api/v1/nodes/n1/overview', headers=headers).json()
        self.assertEqual(overview['settings_operation']['id'], queued['id'])
        self.assertEqual(overview['settings_operation']['status'], 'awaiting_executor')
        self.db.connection.execute("UPDATE backend_node_settings_tasks SET status='running' WHERE id=?", (queued['id'],))
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'NODE_PLANE_SHARED_DIR': directory}):
            write_stage(progress_path(queued['id']), queued['id'], 'protocol_deploy')
            response = self.client.get('/api/v1/node-settings-operations/' + queued['id'], headers=headers)
            self.assertEqual(response.json()['progress']['stage'], 'protocol_deploy')
            overview = self.client.get('/api/v1/nodes/n1/overview', headers=headers).json()
            self.assertEqual(overview['settings_operation']['progress']['stage'], 'protocol_deploy')

    def test_verified_auto_fallback_is_persisted_without_rewriting_command(self):
        self.create()
        with self.db.transaction() as conn:
            settings = json.loads(conn.execute("SELECT settings_json FROM backend_nodes WHERE key='n1'").fetchone()[0])
            settings.update(awg_port=443, awg_port_mode='auto', awg_i1_preset='quic')
            conn.execute("UPDATE backend_nodes SET settings_json=? WHERE key='n1'", (json.dumps(settings),))
        queued = self.queue().json()

        class Driver:
            def inspect_node(self, node_key):
                return {'health_state': 'running', 'xray_config_present': True, 'awg_config_present': True}

            def apply_node_settings(self, command_id, intent):
                effective = dict(intent, settings=dict(intent['settings'], awg_port=8443))
                return json.dumps({'node_key': 'n1', 'revision': 1, 'awg_port': 8443,
                    'settings_sha256': _digest(effective)})

        self.assertTrue(NodeSettingsExecutor(self.db, Driver()).run_one())
        with self.db.connect() as conn:
            task = conn.execute('SELECT intent_json,status FROM backend_node_settings_tasks WHERE id=?', (queued['id'],)).fetchone()
            self.assertEqual(task['status'], 'succeeded')
            self.assertEqual(json.loads(task['intent_json'])['settings']['awg_port'], 443)
            node = conn.execute("SELECT settings_json,applied_revision FROM backend_nodes WHERE key='n1'").fetchone()
            self.assertEqual(json.loads(node['settings_json'])['awg_port'], 8443)
            self.assertEqual(node['applied_revision'], 1)

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

    def test_initial_apply_without_agent_retires_safely_for_bootstrap(self):
        self.assertEqual(self.create().status_code, 201)
        task_id = self.queue().json()['id']

        class Driver:
            def inspect_node(self, node_key):
                raise ConnectionError('agent is offline')

            def apply_node_settings(self, command_id, intent):
                raise AssertionError('mutation must not start')

        self.assertTrue(NodeSettingsExecutor(self.db, Driver()).run_one())
        result = self.client.get(f'/api/v1/node-settings-operations/{task_id}', headers=self.admin_headers()).json()
        self.assertEqual(result['status'], 'superseded')
        self.assertEqual(result['error_code'], 'node_installation_required')

    def test_adding_second_protocol_on_managed_node_does_not_prepare_it_as_fresh(self):
        self.assertEqual(self.create().status_code,201)
        for existing in ('awg','xray'):
            with self.subTest(existing=existing):
                with self.db.transaction() as conn:
                    conn.execute("DELETE FROM backend_node_settings_tasks WHERE node_key='n1'")
                    conn.execute("UPDATE backend_nodes SET applied_revision=1,desired_revision=2 WHERE key='n1'")
                task_id=self.queue(2).json()['id']
                calls=[]
                class Driver:
                    def inspect_node(inner,node_key):
                        return {'health_state':'running','awg_config_present':existing=='awg','xray_config_present':existing=='xray'}
                    def prepare_node(inner,node_key):
                        raise AssertionError('managed runtime cannot be prepared as fresh')
                    def apply_node_settings(inner,command_id,intent):
                        calls.append(command_id)
                        return json.dumps({'node_key':'n1','revision':2,'settings_sha256':_digest(intent)})
                self.assertTrue(NodeSettingsExecutor(self.db,Driver()).run_one())
                self.assertEqual(calls,[task_id])
                result=self.client.get(f'/api/v1/node-settings-operations/{task_id}',headers=self.admin_headers()).json()
                self.assertEqual(result['status'],'succeeded')

    def test_clean_agent_is_prepared_before_settings_command(self):
        self.assertEqual(self.create().status_code, 201)
        self.db.connection.execute("UPDATE backend_nodes SET applied_revision=1, desired_revision=2 WHERE key='n1'")
        self.db.connection.commit()
        task_id = self.queue(2).json()['id']
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
                return json.dumps({'node_key': 'n1', 'revision': 2,
                                   'settings_sha256': _digest(intent)})

        self.assertTrue(NodeSettingsExecutor(self.db, Driver()).run_one())
        self.assertEqual(calls, ['inspect', 'prepare', 'apply'])
        self.assertEqual(self.db.connection.execute('SELECT applied_revision FROM backend_nodes WHERE key = ?',
            ('n1',)).fetchone()['applied_revision'], 2)

    def test_failed_preparation_leaves_command_queued(self):
        self.assertEqual(self.create().status_code, 201)
        self.db.connection.execute("UPDATE backend_nodes SET applied_revision=1, desired_revision=2 WHERE key='n1'")
        self.db.connection.commit()
        task_id = self.queue(2).json()['id']

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
        self.assertEqual(member['id'], 'telegram:102')
        self.assertFalse(member['account_created'])
        guest_headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '102',
                         'Idempotency-Key': str(uuid4()), 'If-Match': '"1"'}
        self.assertEqual(self.client.post('/api/v1/nodes/n1/apply-settings', headers=guest_headers).status_code, 401)
        self.provision(102)
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
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['error']['code'], 'invalid_input')

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

    def test_protocol_removal_settings_gated_by_confirmed_revocations(self):
        from backend.profiles import ProfileRepository
        from backend.grant_policies import materialize
        ProfileRepository(self.db).ensure_account_profile(self.admin.id)
        self.assertEqual(self.create().status_code, 201)
        task_id = self.queue().json()['id']

        class InitialDriver:
            def inspect_node(self, node_key):
                return {'health_state': 'running', 'xray_config_present': True, 'awg_config_present': True}

            def apply_node_settings(self, command_id, intent):
                return json.dumps({'node_key': 'n1', 'revision': 1, 'settings_sha256': _digest(intent)})

        worker = NodeSettingsExecutor(self.db, InitialDriver())
        self.assertTrue(worker.run_one())

        with self.db.transaction() as conn:
            admin_profile = conn.execute("SELECT id FROM backend_profiles WHERE owner_account_id=?", (self.admin.id,)).fetchone()['id']
            materialize(conn, admin_profile)

        # Edit node to remove xray
        response = self.client.patch('/api/v1/nodes/n1', headers=self.admin_headers(**{
            'Idempotency-Key': str(uuid4()), 'If-Match': '"1"'}), json={'protocols': ['awg']})
        self.assertEqual(response.status_code, 200, response.text)

        # Queue apply settings for revision 2
        queued2 = self.queue(revision=2).json()
        task2_id = queued2['id']

        dispatched = []

        class RemovalDriver:
            def inspect_node(self, node_key):
                return {'health_state': 'running', 'awg_config_present': True}

            def apply_node_settings(self, command_id, intent):
                dispatched.append(command_id)
                return json.dumps({'node_key': 'n1', 'revision': 2, 'settings_sha256': _digest(intent)})

        removal_worker = NodeSettingsExecutor(self.db, RemovalDriver())

        # Operation delete task for xray is awaiting_executor, so removal settings MUST NOT dispatch
        self.assertFalse(removal_worker.run_one())
        self.assertEqual(dispatched, [])
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT status FROM backend_node_settings_tasks WHERE id=?', (task2_id,)).fetchone()['status'], 'awaiting_executor')

        # Mark delete task succeeded
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_operation_tasks SET status='succeeded' WHERE node_key='n1' AND protocol='xray'")

        # Now revocations confirmed: settings removal dispatches
        self.assertTrue(removal_worker.run_one())
        self.assertEqual(dispatched, [task2_id])
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT status FROM backend_node_settings_tasks WHERE id=?', (task2_id,)).fetchone()['status'], 'succeeded')
            self.assertEqual(conn.execute('SELECT applied_revision FROM backend_nodes WHERE key=?', ('n1',)).fetchone()['applied_revision'], 2)
