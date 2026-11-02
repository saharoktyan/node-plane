import json
import unittest
from uuid import uuid4

from backend import device_credentials
from backend.devices import DeviceRepository
from backend.operations import OperationRepository
from tests import test_backend_device_commands


class VlessDeviceTests(unittest.TestCase):
    setUp = test_backend_device_commands.BackendDeviceCommandTests.setUp
    headers_for = test_backend_device_commands.BackendDeviceCommandTests.headers_for
    create = test_backend_device_commands.BackendDeviceCommandTests.create
    prepare = test_backend_device_commands.BackendDeviceCommandTests.prepare
    grants = test_backend_device_commands.BackendDeviceCommandTests.grants
    add = test_backend_device_commands.BackendDeviceCommandTests.add

    def xray_tasks(self, operation_id):
        rows = self.db.connection.execute('''SELECT device_id,action,intent_json FROM backend_operation_tasks
            WHERE operation_id=? AND protocol='xray' ORDER BY device_id''', (operation_id,)).fetchall()
        return {row['device_id']: (row['action'], json.loads(row['intent_json'])) for row in rows}

    def credential(self, device_id):
        with self.db.connect() as conn:
            return device_credentials.xray(conn, device_id)

    def test_each_device_is_its_own_xray_client_and_deletion_revokes_only_it(self):
        profile = self.prepare()
        granted = self.grants(profile, 1, [{'node_key': 'node', 'protocol': 'xray'}]).json()
        # Without a device there is no client to provision.
        self.assertEqual(granted['runtime_status'], 'no_targets')
        phone = self.add(profile, 'Phone', 2).json()
        laptop = self.add(profile, 'Laptop', 3).json()
        tasks = self.xray_tasks(laptop['operation_id'])
        self.assertEqual(set(tasks), {phone['device']['id'], laptop['device']['id']})
        clients = {device: intent['xray']['uuid'] for device, (_, intent) in tasks.items()}
        names = {device: intent['runtime_name'] for device, (_, intent) in tasks.items()}
        self.assertEqual(len(set(clients.values())), 2)
        self.assertEqual(len(set(names.values())), 2)
        for device, (action, intent) in tasks.items():
            self.assertEqual(action, 'ensure')
            self.assertEqual(intent['xray']['uuid'], self.credential(device)['uuid'])
        removed = self.client.delete(f'/api/v1/profiles/{profile}/devices/{phone["device"]["id"]}',
            headers=self.headers_for(1)).json()
        tasks = self.xray_tasks(removed['operation_id'])
        self.assertEqual(tasks[phone['device']['id']][0], 'delete')
        self.assertEqual(tasks[phone['device']['id']][1]['runtime_name'], names[phone['device']['id']])
        self.assertNotIn('xray', tasks[phone['device']['id']][1])
        self.assertEqual(tasks[laptop['device']['id']][0], 'ensure')
        self.assertEqual(tasks[laptop['device']['id']][1]['xray']['uuid'], clients[laptop['device']['id']])

    def test_rotation_issues_a_new_uuid_for_the_same_client(self):
        profile = self.prepare()
        self.grants(profile, 1, [{'node_key': 'node', 'protocol': 'xray'}])
        device = self.add(profile, 'Phone', 2).json()['device']
        before = self.credential(device['id'])
        path = f'/api/v1/profiles/{profile}/devices/{device["id"]}/rotate'
        key = str(uuid4())
        rotated = self.client.post(path, headers=self.headers_for(device['revision'], key))
        self.assertEqual(rotated.status_code, 202, rotated.text)
        self.assertEqual(rotated.json()['device']['revision'], device['revision'] + 1)
        after = self.credential(device['id'])
        self.assertNotEqual(after['uuid'], before['uuid'])
        self.assertEqual(after['runtime_name'], before['runtime_name'])
        action, intent = self.xray_tasks(rotated.json()['operation_id'])[device['id']]
        self.assertEqual((action, intent['xray']['uuid'], intent['runtime_name']),
                         ('ensure', after['uuid'], before['runtime_name']))
        # A replay returns the recorded result; a stale revision is refused.
        self.assertEqual(self.client.post(path, headers=self.headers_for(device['revision'], key)).json(), rotated.json())
        self.assertEqual(self.credential(device['id'])['uuid'], after['uuid'])
        self.assertEqual(self.client.post(path, headers=self.headers_for(device['revision'])).status_code, 412)

    def test_issuance_needs_a_device_choice_once_there_are_several(self):
        profile = self.prepare()
        self.grants(profile, 1, [{'node_key': 'node', 'protocol': 'xray'}])
        body = {'node_key': 'node', 'protocol': 'xray', 'transport': 'tcp'}
        path = f'/api/v1/profiles/{profile}/config-issuances'
        refused = self.client.post(path, headers=self.headers_for(), json=body)
        self.assertEqual(refused.json()['error']['code'], 'device_unavailable')
        self.add(profile, 'Phone', 2)
        self.add(profile, 'Laptop', 3)
        refused = self.client.post(path, headers=self.headers_for(), json=body)
        self.assertEqual(refused.json()['error']['code'], 'device_required')

    def legacy_profile(self, *, devices=()):
        """A profile from before VLESS devices: one profile-wide client."""
        profile = self.prepare()
        self.grants(profile, 1, [{'node_key': 'node', 'protocol': 'xray'}])
        runtime = self.db.connection.execute('SELECT runtime_name FROM backend_profiles WHERE id=?', (profile,)).fetchone()[0]
        with self.db.transaction() as conn:
            identity = conn.execute('SELECT xray_uuid FROM backend_profile_identities WHERE profile_id=?',
                                    (profile,)).fetchone()[0]
            operation = conn.execute('SELECT * FROM backend_operations WHERE profile_id=?', (profile,)).fetchone()
            conn.execute('''INSERT INTO backend_operation_tasks(id,operation_id,node_key,protocol,device_id,action,status,intent_json)
                VALUES (?,?,'node','xray','','ensure','succeeded',?)''', (str(uuid4()), operation['id'],
                json.dumps({'runtime_name': runtime, 'xray': {'uuid': identity}})))
            for index, (status, name) in enumerate(devices):
                conn.execute('''INSERT INTO backend_devices(id,profile_id,display_name,runtime_name,status,created_at)
                    VALUES (?,?,?,?,?,?)''', (str(uuid4()), profile, name, runtime if index == 0 else f'd_{index}',
                    status, f'2026-01-0{index + 1}'))
        return profile, runtime, identity

    def adopted(self, profile):
        OperationRepository(self.db).initialize_schema()
        rows = self.db.connection.execute('''SELECT c.*,d.display_name,d.status FROM backend_device_credentials c
            JOIN backend_devices d ON d.id=c.device_id WHERE c.profile_id=?''', (profile,)).fetchall()
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_legacy_client_moves_to_the_default_device_without_a_runtime_change(self):
        profile, runtime, identity = self.legacy_profile(devices=[('active', 'Device 1'), ('active', 'Laptop')])
        row = self.adopted(profile)
        self.assertEqual((row['runtime_name'], json.loads(row['credential_json'])['uuid'], row['display_name']),
                         (runtime, identity, 'Device 1'))
        task = self.db.connection.execute("SELECT device_id FROM backend_operation_tasks WHERE protocol='xray'").fetchone()
        self.assertEqual(task[0], row['device_id'])
        # Adoption runs once; a restart changes nothing.
        self.assertEqual(dict(self.adopted(profile)), dict(row))

    def test_legacy_client_without_devices_gets_a_default_device(self):
        profile, runtime, identity = self.legacy_profile()
        row = self.adopted(profile)
        self.assertEqual((row['runtime_name'], row['display_name'], row['status']), (runtime, 'Device 1', 'active'))
        self.assertEqual(json.loads(row['credential_json'])['uuid'], identity)

    def test_legacy_client_after_deleted_devices_keeps_working_on_a_new_device(self):
        profile, runtime, identity = self.legacy_profile(devices=[('retired', 'Device 1')])
        row = self.adopted(profile)
        self.assertEqual((row['runtime_name'], row['display_name'], row['status']), (runtime, 'Device 1', 'active'))
        self.assertNotEqual(row['device_id'], DeviceRepository(self.db).list_for_profile(profile)[0]['id'])

    def test_pure_adoption_matches_backup_rows(self):
        profiles = [{'id': 'p', 'runtime_name': 'alice', 'created_at': '2026-01-01'}]
        identities = [{'profile_id': 'p', 'xray_uuid': 'u'}]
        grants = [{'profile_id': 'p', 'node_key': 'n', 'protocol': 'xray'}]
        devices, credentials = device_credentials.adopt(profiles, [], identities, grants, [], {'p'}, 'now')
        self.assertEqual([(d['runtime_name'], d['status']) for d in devices], [('alice', 'active')])
        self.assertEqual((credentials[0]['device_id'], credentials[0]['runtime_name']), (devices[0]['id'], 'alice'))
        # Not a legacy profile, or already adopted: nothing to do.
        self.assertEqual(device_credentials.adopt(profiles, [], identities, grants, [], set(), 'now'), ([], []))
        self.assertEqual(device_credentials.adopt(profiles, [], identities, grants, credentials, {'p'}, 'now'), ([], []))
