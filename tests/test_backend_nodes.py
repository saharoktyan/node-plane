from __future__ import annotations

from uuid import uuid4
import unittest

import grpc
from fastapi.testclient import TestClient
from backend.http_api import create_app

from tests.test_backend_http import BackendHTTPTests


class BackendNodeTests(unittest.TestCase):
    setUp = BackendHTTPTests.setUp
    register = BackendHTTPTests.register
    provision = BackendHTTPTests.provision

    def admin_headers(self, **extra):
        return {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101', **extra}

    def create(self, body=None, key=None):
        body = body or {'key': 'lv1', 'title': 'Latvia #1', 'region': 'Latvia',
                        'protocols': ['xray', 'awg'], 'xray_transports': ['tcp', 'xhttp'],
                        'settings': {'public_host': 'lv1.example.test', 'xray_tcp_port': 443}}
        return self.client.post('/api/v1/nodes', headers=self.admin_headers(**{
            'Idempotency-Key': key or str(uuid4())}), json=body)

    def edit(self, revision, body, key=None):
        return self.client.patch('/api/v1/nodes/lv1', headers=self.admin_headers(**{
            'Idempotency-Key': key or str(uuid4()), 'If-Match': f'"{revision}"'}), json=body)

    def test_create_read_and_update_desired_node_without_claiming_runtime(self):
        created = self.create()
        self.assertEqual(created.status_code, 201, created.text)
        node = created.json()
        self.assertFalse(node['enabled'])
        self.assertEqual((node['desired_revision'], node['applied_revision']), (1, 0))
        self.assertEqual(node['settings']['public_host'], 'lv1.example.test')
        self.assertEqual(node['settings']['xray_tcp_port'], 443)
        self.assertEqual(node['settings']['xray_sni'], 'www.cloudflare.com')
        self.assertEqual(node['settings']['xray_xhttp_port'], 8443)
        self.assertEqual(node['settings']['awg_port'], 443)
        self.assertEqual(node['settings']['awg_port_mode'], 'auto')
        self.assertEqual(created.headers['ETag'], '"1"')
        listed = self.client.get('/api/v1/nodes', headers=self.admin_headers())
        self.assertEqual(listed.json()['items'], [node])
        self.assertEqual(self.client.get('/api/v1/nodes/lv1', headers=self.admin_headers()).json(), node)
        changed = self.edit(1, {'title': 'Latvia #2', 'settings': {'public_host': 'new.example.test'}})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()['settings']['public_host'], 'new.example.test')
        self.assertEqual(changed.json()['settings']['xray_host'], 'new.example.test')
        self.assertEqual(changed.json()['settings']['awg_public_host'], 'new.example.test')
        self.assertEqual(changed.json()['desired_revision'], 2)
        self.assertEqual(changed.json()['applied_revision'], 0)
        self.assertFalse(changed.json()['enabled'])
        self.assertEqual(self.edit(1, {'title': 'stale'}).status_code, 412)

    def test_idempotency_key_and_retirement_fence(self):
        key = str(uuid4())
        first = self.create(key=key)
        self.assertEqual(self.create(key=key).json(), first.json())
        conflict = self.create(body={'key': 'other', 'title': 'Other', 'region': 'EU', 'protocols': []}, key=key)
        self.assertEqual(conflict.json()['error']['code'], 'idempotency_conflict')
        self.assertEqual(self.create().json()['error']['code'], 'node_key_conflict')
        edit_key = str(uuid4())
        changed = self.edit(1, {'title': 'New'}, key=edit_key)
        self.assertEqual(self.edit(1, {'title': 'New'}, key=edit_key).json(), changed.json())
        self.assertEqual(self.edit(1, {'title': 'Different'}, key=edit_key).status_code, 409)
        with self.db.transaction() as conn:
            conn.execute('''INSERT INTO backend_node_retirements
                (node_key, actor_id, mode, reason, unfinished_tasks, retired_at)
                VALUES (?, ?, 'registry_only', 'retired by test', 0, '2026-09-28')''', ('old', self.admin.id))
        response = self.create(body={'key': 'old', 'title': 'Old', 'region': 'EU', 'protocols': []})
        self.assertEqual(response.json()['error']['code'], 'node_key_retired')

    def test_key_availability_distinguishes_retired_and_active_numeric_suffixes(self):
        self.assertEqual(self.create().status_code, 201)
        with self.db.transaction() as conn:
            for key in ('lv2', 'lv9', 'lv10', 'plain', 'pad009', '9' * 64):
                conn.execute('''INSERT INTO backend_node_retirements
                    (node_key, actor_id, mode, reason, unfinished_tasks, retired_at)
                    VALUES (?, ?, 'registry_only', 'test', 0, '2026-10-26')''', (key, self.admin.id))
        for key, reason, suggestion in (
                ('lv1', 'node_key_conflict', 'lv3'),
                ('lv9', 'node_key_retired', 'lv11'),
                ('plain', 'node_key_retired', 'plain1'),
                ('pad009', 'node_key_retired', 'pad10')):
            result = self.client.get('/api/v1/nodes/key-availability',
                params={'key': key}, headers=self.admin_headers())
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json(), {'available': False, 'reason': reason, 'suggestion': suggestion})
        result = self.client.get('/api/v1/nodes/key-availability',
            params={'key': '9' * 64}, headers=self.admin_headers()).json()
        self.assertLessEqual(len(result['suggestion']), 64)
        self.assertEqual(self.client.get('/api/v1/nodes/key-availability?key=free',
            headers=self.admin_headers()).json(), {'available': True, 'reason': None, 'suggestion': None})
        self.assertEqual(self.client.get('/api/v1/nodes/key-availability?key=bad.key',
            headers=self.admin_headers()).status_code, 422)
        self.assertEqual(self.client.get('/api/v1/nodes/key-availability?key=lv1').status_code, 401)
        # Read-only availability must not reserve the suggested key.
        self.assertEqual(self.create({'key': 'lv3', 'title': 'New', 'region': 'EU',
            'protocols': [], 'transport': 'ssh', 'ssh_target': 'root@node.example'}).status_code, 201)

    def test_field_validation_is_read_only_and_reuses_persistence_constraints(self):
        original = self.create().json()
        self.assertFalse(original['setup_attempted'])
        for value, expected in (({'title': ''}, 422),
                ({'settings': {'xray_tcp_port': 0}}, 422),
                ({'settings': {'xray_xhttp_path': 'no-slash'}}, 422),
                ({'settings': {'xray_fingerprint': 'invalid'}}, 422),
                ({'settings': {'awg_interface': '123'}}, 422),
                ({'settings': {'xray_xhttp_path': '/assets', 'xray_tcp_port': 1443}}, 200)):
            response = self.client.post('/api/v1/nodes/validate-fields',
                json=value, headers=self.admin_headers())
            self.assertEqual(response.status_code, expected, response.text)
        self.assertEqual(self.client.get('/api/v1/nodes/lv1', headers=self.admin_headers()).json(), original)
        self.assertEqual(self.client.post('/api/v1/nodes/validate-fields', json={'title': 'x'}).status_code, 401)
        self.client.post('/api/v1/nodes/lv1/agent-rollouts',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4())}), json={'transport': 'local'})
        self.assertTrue(self.client.get('/api/v1/nodes/lv1', headers=self.admin_headers()).json()['setup_attempted'])

    def test_partial_settings_preserve_custom_fields_and_update_inherited_hosts(self):
        body = {'key': 'lv1', 'title': 'Custom', 'region': 'EU', 'protocols': ['awg', 'xray'],
            'settings': {'public_host': 'old.example', 'xray_sni': 'example.com',
                'xray_fingerprint': 'firefox', 'xray_tcp_port': 1443, 'xray_xhttp_port': 2443,
                'xray_xhttp_path': '/custom', 'awg_port': 51111, 'awg_interface': 'vpn0',
                'awg_i1_preset': 'dns'}}
        original = self.create(body).json()['settings']
        changed = self.edit(1, {'settings': {'public_host': 'new.example'}})
        self.assertEqual(changed.status_code, 200, changed.text)
        expected = {**original, 'public_host': 'new.example', 'xray_host': 'new.example',
                    'awg_public_host': 'new.example'}
        self.assertEqual(changed.json()['settings'], expected)
        again = self.edit(2, {'settings': {'xray_sni': 'www.example.com'}})
        self.assertEqual(again.json()['settings'], {**expected, 'xray_sni': 'www.example.com'})

    def test_full_editor_payload_updates_inherited_hosts_and_preserves_overrides(self):
        settings = self.create().json()['settings']
        settings.update(public_host='new.example', xray_host='vless.example')
        first = self.edit(1, {'settings': settings})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()['settings']['awg_public_host'], 'new.example')
        self.assertEqual(first.json()['settings']['xray_host'], 'vless.example')
        settings = first.json()['settings']
        settings['public_host'] = 'next.example'
        second = self.edit(2, {'settings': settings})
        self.assertEqual(second.json()['settings']['awg_public_host'], 'next.example')
        self.assertEqual(second.json()['settings']['xray_host'], 'vless.example')

    def test_awg_port_changes_follow_manual_and_automatic_modes(self):
        self.create()
        # A patch to a different field must preserve a previously selected fallback.
        fallback = self.edit(1, {'settings': {'awg_port': 8443, 'awg_port_mode': 'auto'}})
        self.assertEqual(fallback.json()['settings']['awg_port'], 8443)
        host = self.edit(2, {'settings': {'public_host': 'next.example'}})
        self.assertEqual(host.json()['settings']['awg_port'], 8443)
        preset = self.edit(3, {'settings': {'awg_i1_preset': 'dns'}})
        self.assertEqual(preset.json()['settings']['awg_port'], 53)
        manual = self.edit(4, {'settings': {'awg_port': 51111}})
        self.assertEqual(manual.json()['settings']['awg_port_mode'], 'manual')
        kept = self.edit(5, {'settings': {'awg_i1_preset': 'quic'}})
        self.assertEqual(kept.json()['settings']['awg_port'], 51111)
        auto = self.edit(6, {'settings': {'awg_port_mode': 'auto'}})
        self.assertEqual(auto.json()['settings']['awg_port'], 443)
        self.assertEqual(auto.json()['settings']['awg_port_mode'], 'auto')

    def test_invalid_effective_settings_are_rejected_before_saving(self):
        self.create()
        for changes in ({'public_host': 'bad host'}, {'awg_public_host': 'bad;host'},
                        {'xray_xhttp_path': 'missing/slash'}, {'xray_tcp_port': 8443}):
            with self.subTest(changes=changes):
                response = self.edit(1, {'settings': changes})
                self.assertEqual(response.status_code, 422, response.text)
                node = self.client.get('/api/v1/nodes/lv1', headers=self.admin_headers()).json()
                self.assertEqual(node['desired_revision'], 1)

    def test_manual_awg_profile_is_persisted_validated_and_can_return_to_auto(self):
        from awg_parameters import profile_module
        from backend.node_settings import _snapshot
        self.create()
        values = profile_module().new_profile('dns')
        edited = self.edit(1, {'settings': {'awg_parameters': values}})
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual(edited.json()['settings']['awg_parameters'], values)
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM backend_nodes WHERE key='lv1'").fetchone()
            self.assertEqual(_snapshot(row)['settings']['awg_parameters'], values)
        invalid = self.edit(2, {'settings': {'awg_parameters': {**values, 'H2': values['H1']}}})
        self.assertEqual(invalid.status_code, 422, invalid.text)
        self.assertEqual(invalid.json()['error']['code'], 'invalid_awg_parameters')
        automatic = self.edit(2, {'settings': {'awg_parameters': {}}})
        self.assertEqual(automatic.status_code, 200, automatic.text)
        self.assertEqual(automatic.json()['settings']['awg_parameters'], {})

    def test_access_validation_and_active_grant_guard(self):
        visitor = self.register(102).json()
        self.assertFalse(visitor['account_created'])
        member_headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '102'}
        self.assertEqual(self.client.get('/api/v1/nodes', headers=member_headers).status_code, 401)
        self.provision(102)
        self.assertEqual(self.client.get('/api/v1/nodes', headers=member_headers).status_code, 403)
        self.assertEqual(self.client.post('/api/v1/nodes', headers={**member_headers,
            'Idempotency-Key': str(uuid4())}, json={'key': 'foo', 'title': 'Foo', 'region': 'EU', 'protocols': []}).status_code, 403)
        for body in ({'key': '../bad', 'title': 'Bad', 'region': 'EU', 'protocols': []},
                     {'key': 'bad', 'title': 'Bad', 'region': 'EU', 'protocols': ['awg'],
                      'settings': {'awg_port': 70000}},
                     {'key': 'bad', 'title': 'Bad', 'region': 'EU', 'protocols': ['awg'],
                      'xray_transports': ['tcp']}):
            self.assertEqual(self.create(body=body).status_code, 422)
        self.assertEqual(self.create().status_code, 201)
        profile_id = self.db.connection.execute('''INSERT INTO backend_profiles
            (id, runtime_name, display_name) VALUES (?, ?, ?) RETURNING id''',
            (str(uuid4()), 'test', 'Test')).fetchone()['id']
        self.db.connection.execute('INSERT INTO backend_grants(profile_id, node_key, protocol) VALUES (?, ?, ?)',
                                   (profile_id, 'lv1', 'awg'))
        response = self.edit(1, {'protocols': ['xray']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get('/api/v1/nodes/lv1', headers=self.admin_headers()).json()['protocols'], ['xray'])
        with self.db.connect() as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM backend_grants WHERE profile_id=? AND node_key=? AND protocol=?',
                                           (profile_id, 'lv1', 'awg')).fetchone())
        self.assertEqual(self.edit(2, {'settings': {'awg_port': None}}).status_code, 422)
        self.assertEqual(self.client.patch('/api/v1/nodes/lv1', headers=self.admin_headers(**{
            'Idempotency-Key': str(uuid4())}), json={'title': 'No revision'}).status_code, 428)

    def test_runtime_inspection_is_read_only_and_never_acknowledges_settings(self):
        self.assertEqual(self.create().status_code, 201)
        self.db.connection.execute("UPDATE backend_nodes SET applied_revision=1, desired_revision=2 WHERE key='lv1'")
        self.db.connection.commit()

        class Driver:
            calls = []

            def inspect_node(self, node_key):
                self.calls.append(node_key)
                return {'node_key': node_key, 'health_state': 'running',
                        'runtime_version': '0.4.1', 'runtime_commit': 'abc123',
                        'xray_config_present': True, 'awg_config_present': True}

        driver = Driver()
        with TestClient(create_app(self.db, node_driver=driver)) as client:
            response = client.get('/api/v1/nodes/lv1/runtime', headers=self.admin_headers())
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(driver.calls, ['lv1'])
            self.assertFalse(response.json()['settings_verified'])
            self.assertEqual((response.json()['desired_revision'], response.json()['applied_revision']), (2, 1))
            self.assertEqual(client.get('/api/v1/nodes/other/runtime', headers=self.admin_headers()).status_code, 404)
            visitor = self.register(102).json()
            self.assertFalse(visitor['account_created'])
            member_headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '102'}
            self.assertEqual(client.get('/api/v1/nodes/lv1/runtime', headers=member_headers).status_code, 401)
            self.provision(102)
            denied = client.get('/api/v1/nodes/lv1/runtime', headers=member_headers)
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(driver.calls, ['lv1'])

    def test_runtime_inspection_redacts_agent_failure(self):
        self.assertEqual(self.create().status_code, 201)
        self.db.connection.execute("UPDATE backend_nodes SET applied_revision=1, desired_revision=2 WHERE key='lv1'")
        self.db.connection.commit()

        class Unavailable(grpc.RpcError):
            def code(self):
                return grpc.StatusCode.UNAVAILABLE

            def details(self):
                return 'secret agent address'

        class Driver:
            def inspect_node(self, node_key):
                raise Unavailable()

        with TestClient(create_app(self.db, node_driver=Driver())) as client:
            response = client.get('/api/v1/nodes/lv1/runtime', headers=self.admin_headers())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error']['code'], 'node_agent_unavailable')
        self.assertNotIn('secret agent address', response.text)

    def test_uninstalled_node_runtime_and_probe_reject_before_driver_interaction(self):
        created = self.create()
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()['applied_revision'], 0)

        # With default app (node_driver=None), neither runtime nor probe should attempt driver connection
        with TestClient(create_app(self.db, node_driver=None)) as client:
            runtime_resp = client.get('/api/v1/nodes/lv1/runtime', headers=self.admin_headers())
            self.assertEqual(runtime_resp.status_code, 409)
            self.assertEqual(runtime_resp.json()['error']['code'], 'node_installation_required')

            probe_resp = client.get('/api/v1/nodes/lv1/probe', headers=self.admin_headers())
            self.assertEqual(probe_resp.status_code, 409)
            self.assertEqual(probe_resp.json()['error']['code'], 'node_installation_required')

    def test_minimal_creation_is_installable_and_preserves_explicit_parameters(self):
        from backend.node_overview import NodeOverviewService
        from backend.node_settings import _snapshot
        for transport, key in (('local','local1'), ('ssh','remote1')):
            body = {'key':key, 'title':'Node', 'region':'EU', 'transport':transport,
                    'protocols':['awg','xray'], 'settings':{'public_host':'node.example'}}
            if transport == 'ssh':
                body['ssh_target'] = 'root@node.example'
            created = self.create(body).json()
            self.assertEqual(created['xray_transports'], ['tcp','xhttp'])
            with self.db.connect() as conn:
                row = conn.execute('SELECT * FROM backend_nodes WHERE key=?', (key,)).fetchone()
                snapshot = _snapshot(row)
            self.assertEqual(snapshot['settings']['awg_i1_preset'], 'quic')
            self.assertEqual(snapshot['settings']['xray_xhttp_path'], '/assets')
            from backend.authorization import Actor
            actor = Actor(self.credentials.authenticate(self.headers['Authorization']), self.admin)
            self.assertTrue(NodeOverviewService(self.db).get(actor, key)['settings_complete'])
        custom = self.create({'key':'custom','title':'Custom','region':'EU','protocols':['awg','xray'],
            'transport': 'ssh', 'ssh_target': 'root@custom.example',
            'settings':{'public_host':'node.example','awg_port':51111,'awg_i1_preset':'dns',
                        'xray_sni':'example.com','xray_xhttp_path':'/custom'}}).json()['settings']
        self.assertEqual((custom['awg_port'],custom['awg_i1_preset'],custom['xray_sni'],custom['xray_xhttp_path']), (51111,'dns','example.com','/custom'))

    def test_existing_minimal_node_has_defaults_without_changing_applied_revision(self):
        from backend.node_settings import _snapshot
        self.create()
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_nodes SET settings_json=? WHERE key='lv1'", ('{"public_host":"node.example"}',))
            row = conn.execute("SELECT * FROM backend_nodes WHERE key='lv1'").fetchone()
            self.assertEqual(_snapshot(row)['settings']['awg_interface'], 'wg0')
        result = self.client.get('/api/v1/nodes/lv1',headers=self.admin_headers()).json()
        self.assertEqual(result['settings']['xray_tcp_port'],443)
        self.assertEqual(result['applied_revision'],0)


    def test_region_order_preserves_global_order_and_cursor_scope(self):
        definitions = [('a1', 'Zulu', 'Europe'), ('z1', 'Alpha', 'Asia'), ('b1', 'Alpha', 'Europe'), ('b2', 'Alpha', 'Europe')]
        for key, title, region in definitions:
            response = self.create({'key': key, 'title': title, 'region': region, 'protocols': ['awg'],
                'transport': 'ssh', 'ssh_target': 'root@' + key + '.test', 'settings': {'public_host': key + '.test'}})
            self.assertEqual(response.status_code, 201, response.text)
        seen, cursor = [], None
        while True:
            params = {'order': 'region', 'limit': 1}
            if cursor:
                params['cursor'] = cursor
            response = self.client.get('/api/v1/nodes', headers=self.admin_headers(), params=params)
            self.assertEqual(response.status_code, 200, response.text)
            page = response.json()
            seen.extend(item['key'] for item in page['items'])
            cursor = page['next_cursor']
            if not cursor:
                break
            self.assertEqual(self.client.get('/api/v1/nodes', headers=self.admin_headers(), params={'cursor': cursor}).status_code, 422)
        self.assertEqual(seen, ['z1', 'b1', 'b2', 'a1'])
        first = self.client.get('/api/v1/nodes', headers=self.admin_headers(), params={'order': 'region', 'search': 'Alpha', 'limit': 1}).json()
        second = self.client.get('/api/v1/nodes', headers=self.admin_headers(), params={'order': 'region', 'search': 'Alpha', 'cursor': first['next_cursor'], 'limit': 1}).json()
        self.assertEqual([first['items'][0]['key'], second['items'][0]['key']], ['z1', 'b1'])
        self.assertEqual(self.client.get('/api/v1/nodes', headers=self.admin_headers(), params={'order': 'region', 'search': 'Zulu', 'cursor': first['next_cursor']}).status_code, 422)
