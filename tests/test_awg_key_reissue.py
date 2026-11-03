"""Reissuing a device's AWG key: a new peer, the old one revoked everywhere."""
import json
import unittest
from uuid import uuid4

from backend.devices import DeviceRepository
from tests import test_backend_device_commands


class AwgKeyReissueTests(unittest.TestCase):
    setUp = test_backend_device_commands.BackendDeviceCommandTests.setUp
    headers_for = test_backend_device_commands.BackendDeviceCommandTests.headers_for
    create = test_backend_device_commands.BackendDeviceCommandTests.create
    prepare = test_backend_device_commands.BackendDeviceCommandTests.prepare
    grants = test_backend_device_commands.BackendDeviceCommandTests.grants
    add = test_backend_device_commands.BackendDeviceCommandTests.add

    def tasks(self, operation_id, protocol):
        rows = self.db.connection.execute('''SELECT device_id,action,intent_json FROM backend_operation_tasks
            WHERE operation_id=? AND protocol=? ORDER BY device_id''', (operation_id, protocol)).fetchall()
        return {row['device_id']: (row['action'], json.loads(row['intent_json'])['runtime_name']) for row in rows}

    def runtime_name(self, device_id):
        return self.db.connection.execute('SELECT runtime_name FROM backend_devices WHERE id=?',
                                          (device_id,)).fetchone()[0]

    def retired(self):
        return [tuple(row) for row in self.db.connection.execute(
            'SELECT slot_id,device_id,runtime_name FROM backend_retired_peers').fetchall()]

    def reissue(self, profile, device, revision, key=None, protocol='awg'):
        return self.client.post(f'/api/v1/profiles/{profile}/devices/{device}/rotate',
                                headers=self.headers_for(revision, key), json={'protocol': protocol})

    def setup_profile(self):
        profile = self.prepare()
        self.grants(profile, 1, [{'node_key': 'node', 'protocol': 'awg'}, {'node_key': 'node', 'protocol': 'xray'}])
        phone = self.add(profile, 'Phone', 2).json()['device']
        laptop = self.add(profile, 'Laptop', 3).json()['device']
        return profile, phone, laptop

    def test_reissue_creates_a_new_peer_and_revokes_the_old_one(self):
        profile, phone, laptop = self.setup_profile()
        old = self.runtime_name(phone['id'])
        xray_before = self.db.connection.execute(
            "SELECT credential_json FROM backend_device_credentials WHERE device_id=?", (phone['id'],)).fetchone()[0]
        key = str(uuid4())
        result = self.reissue(profile, phone['id'], phone['revision'], key)
        self.assertEqual(result.status_code, 202, result.text)
        self.assertEqual(result.json()['device']['revision'], phone['revision'] + 1)
        new = self.runtime_name(phone['id'])
        self.assertNotEqual(new, old)
        [(slot, device_id, name)] = self.retired()
        self.assertEqual((device_id, name), (phone['id'], old))
        awg = self.tasks(result.json()['operation_id'], 'awg')
        self.assertEqual(awg[phone['id']], ('ensure', new))
        self.assertEqual(awg[slot], ('delete', old))
        self.assertEqual(awg[laptop['id']][0], 'ensure')
        # The VLESS client of the device does not change.
        self.assertEqual(self.db.connection.execute(
            "SELECT credential_json FROM backend_device_credentials WHERE device_id=?", (phone['id'],)).fetchone()[0],
            xray_before)
        operation = self.client.get('/api/v1/operations/' + result.json()['operation_id'], headers=self.headers_for())
        self.assertEqual(operation.status_code, 200, operation.text)
        # Replays return the result; the same key for another protocol conflicts.
        self.assertEqual(self.reissue(profile, phone['id'], phone['revision'], key).json(), result.json())
        self.assertEqual(self.reissue(profile, phone['id'], phone['revision'], key, 'xray').status_code, 409)
        # A later operation still revokes the old peer until a node confirms it.
        later = self.add(profile, 'Tablet', result.json()['profile_revision']).json()
        self.assertEqual(self.tasks(later['operation_id'], 'awg')[slot], ('delete', old))
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_operation_tasks SET status='succeeded' WHERE device_id=?", (slot,))
            DeviceRepository.settle_deletions(conn)
        self.assertEqual(self.retired(), [])

    def test_a_second_reissue_keeps_both_old_peers_until_revoked(self):
        profile, phone, _ = self.setup_profile()
        first = self.reissue(profile, phone['id'], phone['revision']).json()
        self.reissue(profile, phone['id'], first['device']['revision'])
        self.assertEqual(len(self.retired()), 2)
        names = {self.runtime_name(phone['id'])} | {name for _, _, name in self.retired()}
        self.assertEqual(len(names), 3)

    def test_without_awg_targets_nothing_waits_for_revocation(self):
        profile = self.prepare()
        self.grants(profile, 1, [{'node_key': 'node', 'protocol': 'xray'}])
        phone = self.add(profile, 'Phone', 2).json()['device']
        self.assertEqual(self.reissue(profile, phone['id'], phone['revision']).status_code, 202)
        self.assertEqual(self.retired(), [])

    def test_unknown_protocols_are_refused(self):
        profile, phone, _ = self.setup_profile()
        self.assertEqual(self.reissue(profile, phone['id'], phone['revision'], protocol='wireguard').status_code, 422)


if __name__ == '__main__':
    unittest.main()
