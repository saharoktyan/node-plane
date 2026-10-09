import unittest
from uuid import uuid4

from tests import test_backend_nodes


class InstallationDefaultsTests(unittest.TestCase):
    setUp = test_backend_nodes.BackendNodeTests.setUp
    admin_headers = test_backend_nodes.BackendNodeTests.admin_headers
    create = test_backend_nodes.BackendNodeTests.create

    def update(self, body, revision=1):
        return self.client.put('/api/v1/system/installation-defaults',
            headers=self.admin_headers(**{'If-Match': f'"{revision}"'}), json=body)

    def test_xray_defaults_are_complete_and_preserve_custom_values(self):
        expected = {'xray_sni': 'www.cloudflare.com', 'xray_fingerprint': 'chrome',
            'xray_tcp_port': 443, 'xray_xhttp_port': 8443, 'xray_xhttp_path': '/assets'}
        first = self.client.get('/api/v1/system/installation-defaults', headers=self.admin_headers()).json()
        self.assertEqual({k: first['settings'][k] for k in expected}, expected)
        self.assertNotIn('awg_port', first['settings'])
        body = {'protocols': ['awg', 'xray'], 'xray_transports': ['tcp'],
            'settings': {'awg_port_mode': 'auto', 'xray_sni': 'example.com'}}
        saved = self.update(body).json()
        self.assertEqual(saved['settings']['xray_sni'], 'example.com')
        self.assertEqual(saved['settings']['xray_tcp_port'], 443)
        self.assertEqual(self.update(body).json(), saved)
        loaded = self.client.get('/api/v1/system/installation-defaults', headers=self.admin_headers()).json()
        self.assertEqual(loaded, saved)

    def test_local_slot_blocks_disabled_nodes_and_transport_conversion(self):
        self.assertTrue(self.client.get('/api/v1/nodes/creation-options', headers=self.admin_headers()).json()['local_available'])
        first = self.create()
        self.assertEqual(first.status_code, 201, first.text)
        self.assertFalse(first.json()['enabled'])
        self.assertFalse(self.client.get('/api/v1/nodes/creation-options', headers=self.admin_headers()).json()['local_available'])
        body = {'key': 'second', 'title': 'Second', 'region': 'Europe', 'protocols': ['awg'],
            'settings': {'public_host': 'second.example'}}
        response = self.create(body)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'local_node_exists')
        body.update(transport='ssh', ssh_target='root@second.example')
        response = self.create(body)
        self.assertEqual(response.status_code, 201, response.text)
        conversion = self.client.patch('/api/v1/nodes/second',
            headers=self.admin_headers(**{'If-Match': '"1"', 'Idempotency-Key': str(uuid4())}),
            json={'transport': 'local', 'ssh_target': None})
        self.assertEqual(conversion.status_code, 409, conversion.text)

    def test_defaults_only_affect_future_nodes_and_explicit_overrides_win(self):
        original = self.create().json()
        body = {'protocols': ['awg', 'xray'], 'xray_transports': ['xhttp'],
            'settings': {'awg_i1_preset': 'dns', 'awg_port_mode': 'auto', 'xray_sni': 'example.com'}}
        response = self.update(body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.update(body).json(), response.json())
        self.assertEqual(self.client.get('/api/v1/nodes/lv1', headers=self.admin_headers()).json(), original)
        future = self.create({'key': 'future', 'title': 'Future', 'region': 'EU', 'protocols': ['awg', 'xray'],
            'transport': 'ssh', 'ssh_target': 'root@future.example', 'settings': {'public_host': 'future.example'}}).json()
        self.assertEqual(future['xray_transports'], ['xhttp'])
        self.assertEqual((future['settings']['awg_i1_preset'], future['settings']['awg_port']), ('dns', 53))
        override = self.create({'key': 'override', 'title': 'Override', 'region': 'EU', 'protocols': ['awg'],
            'transport': 'ssh', 'ssh_target': 'root@override.example',
            'settings': {'public_host': 'override.example', 'awg_i1_preset': 'chaos', 'awg_port': 51111}}).json()
        self.assertEqual((override['settings']['awg_port'], override['settings']['awg_port_mode']), (51111, 'manual'))

    def test_invalid_portable_settings_and_conflicting_revision_are_rejected(self):
        body = {'protocols': ['awg'], 'xray_transports': [], 'settings': {'awg_i1_preset': 'dns', 'awg_port_mode': 'auto'}}
        self.assertEqual(self.update(body).status_code, 200)
        changed = {**body, 'settings': {'awg_port_mode': 'auto', 'awg_i1_preset': 'quic'}}
        self.assertEqual(self.update(changed).status_code, 412)
        for settings in ({'public_host': 'not-portable.example'}, {'awg_port_mode': 'manual'},
                {'awg_port_mode': 'auto', 'awg_port': 5000}, {'xray_sni': 'not a host'}):
            response = self.update({**body, 'settings': settings}, 2)
            self.assertEqual(response.status_code, 422, response.text)

    def test_create_replay_does_not_hit_local_slot_guard_again(self):
        key = str(uuid4())
        first = self.create(key=key)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(self.create(key=key).json(), first.json())

    def test_only_admin_can_change_installation_defaults(self):
        member = self.identities.resolve_telegram(102)
        self.db.connection.execute("UPDATE backend_accounts SET status='approved' WHERE id=?", (member.id,))
        headers = {**self.admin_headers(), 'X-Node-Plane-Telegram-User-ID': '102'}
        response = self.client.get('/api/v1/system/installation-defaults', headers=headers)
        self.assertEqual(response.status_code, 403)

    def test_shared_templates_allocate_clean_codes_and_replay_original_result(self):
        options = self.client.get('/api/v1/nodes/creation-options', headers=self.admin_headers()).json()
        template = next(t for t in options['templates'] if t['code'] == 'lv')
        self.assertEqual(template['draft']['key'], 'lv1')
        body = {**template['draft'], 'protocols': ['awg'], 'transport': 'ssh',
            'ssh_target': 'root@vpn.example', 'settings': {'public_host': 'vpn.example'}}
        identity = str(uuid4())
        first = self.create(body, key=identity)
        self.assertEqual(first.status_code, 201, first.text)
        second = self.create(body)
        self.assertEqual(second.status_code, 201, second.text)
        self.assertEqual((first.json()['key'], second.json()['key']), ('lv1', 'lv2'))
        self.assertEqual(second.json()['title'], 'Latvia #2')
        self.assertEqual(self.create(body, key=identity).json(), first.json())
        self.assertFalse(first.json()['enabled'])
        self.assertEqual(first.json()['applied_revision'], 0)

    def test_template_numbers_skip_retirements_and_legacy_suffixes(self):
        with self.db.transaction() as conn:
            for node_key in ('lv3', 'lv9-1234abcd'):
                conn.execute('''INSERT INTO backend_node_retirements
                    (node_key, actor_id, mode, reason, unfinished_tasks, retired_at)
                    VALUES (?, ?, 'registry_only', 'retired by test', 0, '2026-09-28')''', (node_key, self.admin.id))
        options = self.client.get('/api/v1/nodes/creation-options', headers=self.admin_headers()).json()
        template = next(t for t in options['templates'] if t['code'] == 'lv')
        self.assertEqual(template['draft']['key'], 'lv10')
        response = self.create({**template['draft'], 'protocols': ['awg']})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()['key'], 'lv10')
        malformed = self.create({**template['draft'], 'template': 'unknown', 'protocols': ['awg']})
        self.assertEqual(malformed.status_code, 422, malformed.text)
