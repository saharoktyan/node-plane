import json
import unittest

from backend.devices import DeviceRepository
from backend.operations import OperationRepository
from tests import test_backend_operations


class BackendDeviceIdentityTests(unittest.TestCase):
    setUp = test_backend_operations.BackendOperationTests.setUp
    headers_for = test_backend_operations.BackendOperationTests.headers_for
    create = test_backend_operations.BackendOperationTests.create
    prepare = test_backend_operations.BackendOperationTests.prepare
    grants = test_backend_operations.BackendOperationTests.grants

    def test_first_awg_grant_adopts_identity_without_extra_tasks(self):
        profile_id = self.prepare()
        result = self.grants(profile_id, 1, [{'node_key': 'node', 'protocol': 'awg'}])
        self.assertEqual(result.status_code, 200, result.text)
        row = self.db.connection.execute('SELECT * FROM backend_devices WHERE profile_id=?', (profile_id,)).fetchone()
        profile = self.db.connection.execute('SELECT * FROM backend_profiles WHERE id=?', (profile_id,)).fetchone()
        self.assertEqual(row['runtime_name'], profile['runtime_name'])
        self.assertEqual(row['display_name'], 'Device 1')
        tasks = self.db.connection.execute('SELECT intent_json FROM backend_operation_tasks').fetchall()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(json.loads(tasks[0]['intent_json'])['runtime_name'], row['runtime_name'])
        OperationRepository(self.db).initialize_schema()
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_devices').fetchone()[0], 1)

    def test_xray_only_profile_has_no_automatic_device(self):
        profile_id = self.prepare()
        self.grants(profile_id, 1, [{'node_key': 'node', 'protocol': 'xray'}])
        self.assertEqual(DeviceRepository(self.db).list_for_profile(profile_id), [])

    def test_migration_adopts_historical_awg_after_grants_removed(self):
        profile_id = self.prepare()
        self.grants(profile_id, 1, [{'node_key': 'node', 'protocol': 'awg'}])
        self.grants(profile_id, 2, [])
        self.db.connection.execute('DELETE FROM backend_devices')
        before = self.db.connection.execute('SELECT intent_json FROM backend_operation_tasks ORDER BY id').fetchall()
        OperationRepository(self.db).initialize_schema()
        device = DeviceRepository(self.db).list_for_profile(profile_id)[0]
        OperationRepository(self.db).initialize_schema()
        self.assertEqual(DeviceRepository(self.db).list_for_profile(profile_id)[0]['id'], device['id'])
        after = self.db.connection.execute('SELECT intent_json FROM backend_operation_tasks ORDER BY id').fetchall()
        self.assertEqual([row[0] for row in before], [row[0] for row in after])

    def test_retired_device_is_not_recreated_on_profile_edit(self):
        profile_id = self.prepare()
        self.grants(profile_id, 1, [{'node_key': 'node', 'protocol': 'awg'}])
        self.db.connection.execute("UPDATE backend_devices SET status='retired'")
        OperationRepository(self.db).initialize_schema()
        with self.db.transaction() as conn:
            profile = conn.execute('SELECT * FROM backend_profiles WHERE id=?', (profile_id,)).fetchone()
            DeviceRepository.ensure_default(conn, profile)
        rows = self.db.connection.execute('SELECT status FROM backend_devices').fetchall()
        self.assertEqual([row[0] for row in rows], ['retired'])

    def test_list_authorizes_parent_and_hides_runtime_identity(self):
        profile_id = self.prepare()
        self.grants(profile_id, 1, [{'node_key': 'node', 'protocol': 'awg'}])
        path = f'/api/v1/profiles/{profile_id}/devices'
        response = self.client.get(path, headers=self.headers_for())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['items'][0]['profile_id'], profile_id)
        self.assertNotIn('runtime_name', response.text)
        self.assertNotIn('operating_system', response.text)
        member = self.identities.resolve_telegram(102)
        self.db.connection.execute("UPDATE backend_accounts SET status='approved' WHERE id=?", (member.id,))
        headers = {**self.headers_for(), 'X-Node-Plane-Telegram-User-ID': '102'}
        self.assertEqual(self.client.get(path, headers=headers).status_code, 404)
        self.db.connection.execute('UPDATE backend_profiles SET owner_account_id=? WHERE id=?', (member.id, profile_id))
        self.assertEqual(self.client.get(path, headers=headers).status_code, 200)
        self.db.connection.execute("UPDATE backend_accounts SET status='pending' WHERE id=?", (member.id,))
        self.assertEqual(self.client.get(path, headers=headers).status_code, 403)
