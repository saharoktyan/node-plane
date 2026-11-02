import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from uuid import uuid4

from backend import vpn_policies
from backend.authorization import Actor, Account, Principal, PrincipalKind, APPROVED_PERMISSIONS, AccessDenied
from backend.backups import encoded
from backend.profile_commands import ProfileCommands
from backend.vpn_policies import PolicyKey, VpnPolicies, effective, integer, mode, register
from tests import test_backend_node_jobs as fixture
from tests.test_backend_backups import BackupDatabase, TestService


class VpnPolicyTests(unittest.TestCase):
    def setUp(self):
        fixture.BackendNodeJobTests.setUp(self)
        saved = dict(vpn_policies.REGISTRY)
        self.addCleanup(lambda: (vpn_policies.REGISTRY.clear(), vpn_policies.REGISTRY.update(saved)))
        register(PolicyKey('test.limit', frozenset({'installation', 'profile', 'device'}),
                           frozenset({'global', 'node', 'node_protocol'}), integer(1, 10), default=5))
        register(PolicyKey('test.mode', frozenset({'profile'}), frozenset({'global'}), mode, default='off',
                           protocols=frozenset({'xray'})))
        self.service = VpnPolicies(self.db)
        self.profile = ProfileCommands(self.db).execute(self.actor, str(uuid4()), action='create',
            values={'display_name': 'Alice'})['profile']['id']
        self.device = str(uuid4())
        self.db.connection.execute('''INSERT INTO backend_devices(id,profile_id,display_name,runtime_name,status,created_at)
            VALUES (?,?,?,?,'active',?)''', (self.device, self.profile, 'Phone', 'd_' + uuid4().hex,
                                            datetime.now(timezone.utc).isoformat()))
        self.db.connection.commit()

    def put(self, subject_kind, subject_id, *changes, key=None):
        return self.service.set(self.actor, key or str(uuid4()), subject_kind, subject_id, list(changes))

    @staticmethod
    def change(value, revision=0, node_key=None, protocol=None, key='test.limit'):
        return {'key': key, 'value': value, 'revision': revision, 'node_key': node_key, 'protocol': protocol}

    def resolve(self, **target):
        with self.db.connect() as conn:
            return effective(conn, keys=['test.limit'], **target)['test.limit']

    def test_specific_scopes_and_subjects_override_broader_values(self):
        self.assertEqual(self.resolve(profile_id=self.profile, node_key='n1', protocol='xray'), 5)
        self.put('installation', None, self.change(2), self.change(3, node_key='n1'))
        self.assertEqual(self.resolve(profile_id=self.profile), 2)
        self.assertEqual(self.resolve(profile_id=self.profile, node_key='n1'), 3)
        self.put('profile', self.profile, self.change(4))
        self.assertEqual(self.resolve(profile_id=self.profile, node_key='n1'), 4)
        self.put('profile', self.profile, self.change(6, node_key='n1', protocol='xray'))
        self.assertEqual(self.resolve(profile_id=self.profile, node_key='n1', protocol='xray'), 6)
        self.assertEqual(self.resolve(profile_id=self.profile, node_key='n1', protocol='awg'), 4)
        self.put('device', self.device, self.change(1))
        self.assertEqual(self.resolve(device_id=self.device, node_key='n1', protocol='xray'), 1)
        # Another profile only inherits installation values.
        other = ProfileCommands(self.db).execute(self.actor, str(uuid4()), action='create',
            values={'display_name': 'Bob'})['profile']['id']
        self.assertEqual(self.resolve(profile_id=other, node_key='n1'), 3)
        with self.db.connect() as conn, self.assertRaises(ValueError):
            effective(conn, profile_id=other, device_id=self.device)

    def test_revisions_updates_and_removal(self):
        first = self.put('profile', self.profile, self.change(4))
        self.assertEqual([(v['value'], v['revision']) for v in first['values']], [(4, 1)])
        with self.assertRaises(AccessDenied) as error:
            self.put('profile', self.profile, self.change(7, revision=0))
        self.assertEqual(error.exception.code, 'revision_conflict')
        self.assertEqual(self.put('profile', self.profile, self.change(7, revision=1))['values'][0]['revision'], 2)
        self.assertEqual(self.put('profile', self.profile, self.change(None, revision=2))['values'], [])
        self.assertEqual(self.resolve(profile_id=self.profile), 5)

    def test_invalid_changes_are_rejected_atomically(self):
        cases = [
            self.change(11),
            self.change(True),
            self.change(1, protocol='xray'),
            self.change('enforce', key='test.mode', node_key='n1'),
            self.change('enforce', key='test.mode', protocol='awg', node_key='n1'),
            {'key': 'unknown', 'value': 1, 'revision': 0},
            {'key': 'test.limit', 'value': 1},
            {**self.change(1), 'extra': True},
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(AccessDenied):
                self.put('profile', self.profile, self.change(2), case)
        with self.assertRaises(AccessDenied):
            self.put('installation', None, self.change('observe', key='test.mode'))
        with self.assertRaises(AccessDenied) as error:
            self.put('profile', self.profile, self.change(2, node_key='missing'))
        self.assertEqual(error.exception.code, 'resource_not_found')
        with self.assertRaises(AccessDenied):
            self.put('profile', self.profile, self.change(2), self.change(3))
        self.assertEqual(self.service.get(self.actor, 'profile', self.profile)['values'], [])

    def test_commands_are_idempotent_and_reject_changed_payloads(self):
        key = str(uuid4())
        first = self.put('profile', self.profile, self.change(3), key=key)
        self.assertEqual(self.put('profile', self.profile, self.change(3), key=key), first)
        with self.assertRaises(AccessDenied) as error:
            self.put('profile', self.profile, self.change(4), key=key)
        self.assertEqual(error.exception.code, 'idempotency_conflict')

    def test_subjects_must_exist_and_writers_need_permissions(self):
        for subject_kind, subject_id in (('profile', str(uuid4())), ('device', str(uuid4())), ('nobody', None)):
            with self.subTest(subject=subject_kind), self.assertRaises(AccessDenied):
                self.put(subject_kind, subject_id, self.change(2))
        self.db.connection.execute("UPDATE backend_devices SET status='retired' WHERE id=?", (self.device,))
        with self.assertRaises(AccessDenied):
            self.put('device', self.device, self.change(2))
        member = Actor(Principal('member', PrincipalKind.ACCOUNT, APPROVED_PERMISSIONS),
                       Account(self.actor.account.id, 'member', 'approved'))
        for subject_kind, subject_id in (('installation', None), ('profile', self.profile)):
            with self.subTest(member=subject_kind), self.assertRaises(AccessDenied):
                self.service.set(member, str(uuid4()), subject_kind, subject_id, [self.change(2)])
            with self.assertRaises(AccessDenied):
                self.service.get(member, subject_kind, subject_id)

    def test_backup_keeps_values_and_old_snapshots_gain_an_empty_table(self):
        self.put('installation', None, self.change(2, node_key='n1'))
        self.put('profile', self.profile, self.change(4))
        self.put('device', self.device, self.change(1))
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        from backend.access_requests import AccessRequestService
        AccessRequestService(self.db).initialize_schema()
        backups = TestService(BackupDatabase(self.db), directory.name)
        backup_id = backups.create_snapshot()['backup_id']
        rows = backups._load(backup_id)['tables']['backend_vpn_policies']['rows']
        self.assertEqual(sorted(row['subject_kind'] for row in rows), ['device', 'installation', 'profile'])
        self.assertTrue(backups.detail(self.actor, backup_id)['compatible'])
        path = backups._path(backup_id)
        payload = json.loads(path.read_bytes())
        del payload['tables']['backend_vpn_policies']
        del payload['tables']['backend_device_credentials']
        payload['format'] = 'node-plane-backend-v3'
        payload['checksum'] = hashlib.sha256(encoded(payload['tables'])).hexdigest()
        path.write_bytes(encoded(payload))
        converted = backups._load(backup_id)
        self.assertEqual(converted['tables']['backend_vpn_policies'],
                         {'columns': vpn_policies.TABLE_COLUMNS, 'rows': []})
        self.assertTrue(backups.detail(self.actor, backup_id)['compatible'])


    def test_bittorrent_policy_marks_xray_nodes_and_reaches_settings_intents(self):
        from backend.node_settings import NodeSettingsService
        from backend.nodes import NodeService
        NodeService(self.db).command(self.actor, action='create', command_key=str(uuid4()), values={
            'key': 'awg1', 'title': 'AWG', 'region': 'EU', 'transport': 'ssh', 'protocols': ['awg'],
            'ssh_target': 'root@awg.example', 'settings': {'public_host': 'awg.example', 'awg_port': 51820}})
        def revisions():
            return {row['key']: row['desired_revision'] for row in
                    self.db.connection.execute('SELECT key,desired_revision FROM backend_nodes').fetchall()}
        before = revisions()
        with self.assertRaises(AccessDenied):
            self.put('profile', self.profile, self.change('block', key='xray.bittorrent'))
        self.put('installation', None, self.change('block', key='xray.bittorrent'))
        after = revisions()
        self.assertEqual(after['n1'], before['n1'] + 1)
        self.assertEqual(after['awg1'], before['awg1'])
        task = NodeSettingsService(self.db).queue(self.actor, 'n1', revision=after['n1'], command_key=str(uuid4()))
        intent = json.loads(self.db.connection.execute('SELECT intent_json FROM backend_node_settings_tasks WHERE id=?',
                                                       (task['id'],)).fetchone()['intent_json'])
        self.assertEqual(intent['settings']['xray_bittorrent'], 'block')
        # The default is omitted, so older agents keep accepting ordinary settings.
        self.put('installation', None, self.change('allow', revision=1, key='xray.bittorrent'))
        with self.db.connect() as conn:
            self.assertEqual(vpn_policies.runtime_settings(conn, 'n1', ['awg', 'xray']), {})
            self.assertEqual(vpn_policies.runtime_settings(conn, 'awg1', ['awg']), {})


class VpnPolicyHttpTests(unittest.TestCase):
    def setUp(self):
        from tests import test_backend_http as http_fixture
        http_fixture.BackendHTTPTests.setUp(self)
        self.admin_headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101'}

    def test_installation_policies_are_listed_and_changed_with_idempotency_keys(self):
        response = self.client.get('/api/v1/vpn-policies/installation', headers=self.admin_headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn({'key': 'xray.bittorrent', 'value': None, 'revision': 0, 'default': 'allow',
                       'effective': 'allow'}, response.json()['keys'])
        body = {'changes': [{'key': 'xray.bittorrent', 'value': 'block', 'revision': 0}]}
        key = str(uuid4())
        for _ in range(2):
            response = self.client.patch('/api/v1/vpn-policies/installation', json=body,
                                         headers={**self.admin_headers, 'Idempotency-Key': key})
            self.assertEqual(response.status_code, 200, response.text)
            current = next(item for item in response.json()['keys'] if item['key'] == 'xray.bittorrent')
            self.assertEqual(current['effective'], 'block')
        for invalid in ({'changes': []}, {'changes': [{'key': 'xray.bittorrent', 'value': 'maybe', 'revision': 1}]},
                        {'changes': [{'key': 'xray.bittorrent', 'value': 'allow', 'revision': True}]}):
            self.assertEqual(self.client.patch('/api/v1/vpn-policies/installation', json=invalid,
                headers={**self.admin_headers, 'Idempotency-Key': str(uuid4())}).status_code, 422)
        self.assertEqual(self.client.patch('/api/v1/vpn-policies/installation', json=body,
            headers=self.admin_headers).status_code, 422)
        from tests import test_backend_http as http_fixture
        http_fixture.BackendHTTPTests.provision(self, 102)
        member = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '102'}
        self.assertEqual(self.client.get('/api/v1/vpn-policies/installation', headers=member).status_code, 403)

if __name__ == '__main__':
    unittest.main()
