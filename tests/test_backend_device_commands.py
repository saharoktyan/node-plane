import json
import unittest
from uuid import uuid4

from backend.devices import DeviceRepository
from tests import test_backend_devices


class BackendDeviceCommandTests(unittest.TestCase):
    setUp = test_backend_devices.BackendDeviceIdentityTests.setUp
    headers_for = test_backend_devices.BackendDeviceIdentityTests.headers_for
    create = test_backend_devices.BackendDeviceIdentityTests.create
    prepare = test_backend_devices.BackendDeviceIdentityTests.prepare
    grants = test_backend_devices.BackendDeviceIdentityTests.grants

    def add(self, profile, name, revision, key=None):
        return self.client.post(f'/api/v1/profiles/{profile}/devices',
            headers=self.headers_for(revision, key), json={'display_name': name})

    def test_custom_name_identity_and_idempotent_creation(self):
        profile = self.prepare()
        key = str(uuid4())
        first = self.add(profile, '  My   laptop  ', 1, key)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(first.json()['device']['display_name'], 'My laptop')
        self.assertNotIn('runtime_name', first.text)
        self.assertEqual(self.add(profile, '  My   laptop  ', 1, key).json(), first.json())
        self.assertEqual(self.add(profile, 'Phone', 1, key).status_code, 409)
        second = self.add(profile, 'Phone', 2)
        self.assertEqual(second.status_code, 201, second.text)
        rows = self.db.connection.execute('SELECT runtime_name FROM backend_devices').fetchall()
        self.assertEqual(len({r[0] for r in rows}), 2)

    def test_names_are_unique_within_profile_after_normalization(self):
        profile = self.prepare()
        self.assertEqual(self.add(profile, 'My Laptop', 1).status_code, 201)
        for name in ('my laptop', '  MY    LAPTOP ', 'Ｍｙ Laptop'):
            response = self.add(profile, name, 2)
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()['error']['code'], 'device_name_conflict')
        other = self.create().json()['profile']['id']
        self.assertEqual(self.add(other, 'My Laptop', 1).status_code, 201)

    def test_invalid_names_and_stale_revision_leave_no_changes(self):
        profile = self.prepare()
        for name in ('   ', '\u200b', 'x' * 65, 'hello\nworld'):
            self.assertEqual(self.add(profile, name, 1).status_code, 422)
        self.assertEqual(self.add(profile, 'Laptop', 2).status_code, 412)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_devices').fetchone()[0], 0)

    def test_rename_preserves_runtime_and_does_not_create_remote_tasks(self):
        profile = self.prepare()
        self.grants(profile, 1, [{'node_key': 'node', 'protocol': 'awg'}])
        device = DeviceRepository(self.db).list_for_profile(profile)[0]
        before = list(self.db.connection.execute('SELECT intent_json FROM backend_operation_tasks'))
        runtime = self.db.connection.execute('SELECT runtime_name FROM backend_devices').fetchone()[0]
        result = self.client.patch(f'/api/v1/profiles/{profile}/devices/{device["id"]}',
            headers=self.headers_for(1), json={'display_name': 'My router'})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['runtime_status'], 'metadata_only')
        self.assertEqual(result.json()['profile_revision'], 2)
        self.assertEqual(result.json()['device']['revision'], 2)
        self.assertEqual(self.db.connection.execute('SELECT runtime_name FROM backend_devices').fetchone()[0], runtime)
        self.assertEqual([r[0] for r in before], [r[0] for r in self.db.connection.execute('SELECT intent_json FROM backend_operation_tasks')])

    def test_delete_waits_for_all_remote_revocations(self):
        profile = self.prepare()
        self.grants(profile, 1, [{'node_key': 'node', 'protocol': 'awg'}])
        device = DeviceRepository(self.db).list_for_profile(profile)[0]
        path = f'/api/v1/profiles/{profile}/devices/{device["id"]}'
        result = self.client.delete(path, headers=self.headers_for(1))
        self.assertEqual(result.status_code, 202, result.text)
        self.assertEqual(result.json()['device']['status'], 'deleting')
        tasks = self.db.connection.execute('SELECT action,intent_json FROM backend_operation_tasks WHERE operation_id=?', (result.json()['operation_id'],)).fetchall()
        self.assertEqual([r['action'] for r in tasks], ['delete'])
        self.assertEqual(json.loads(tasks[0]['intent_json'])['runtime_name'],
            self.db.connection.execute('SELECT runtime_name FROM backend_devices').fetchone()[0])
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_operation_tasks SET status='superseded' WHERE action='ensure'")
            conn.execute("UPDATE backend_operation_tasks SET status='blocked' WHERE action='delete'")
            DeviceRepository.settle_deletions(conn)
        self.assertEqual(DeviceRepository(self.db).list_for_profile(profile)[0]['status'], 'deleting')
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_operation_tasks SET status='succeeded' WHERE action='delete'")
            DeviceRepository.settle_deletions(conn)
        self.assertEqual(DeviceRepository(self.db).list_for_profile(profile)[0]['status'], 'retired')
        self.assertEqual(self.add(profile, 'Device 1', 3).status_code, 201)

    def test_never_provisioned_device_retires_immediately(self):
        profile = self.prepare()
        device = self.add(profile, 'Phone', 1).json()['device']
        result = self.client.delete(f'/api/v1/profiles/{profile}/devices/{device["id"]}', headers=self.headers_for(1))
        self.assertEqual(result.status_code, 202, result.text)
        self.assertEqual(result.json()['device']['status'], 'retired')

    def test_foreign_profile_mutation_is_denied(self):
        profile = self.prepare()
        member = self.identities.resolve_telegram(102)
        self.db.connection.execute("UPDATE backend_accounts SET status='approved' WHERE id=?", (member.id,))
        headers = {**self.headers_for(1), 'X-Node-Plane-Telegram-User-ID': '102'}
        result = self.client.post(f'/api/v1/profiles/{profile}/devices', headers=headers, json={'display_name': 'Phone'})
        self.assertEqual(result.status_code, 404)
