import json
import unittest
from uuid import uuid4

from tests import test_backend_profile_commands


class BackendOperationTests(unittest.TestCase):
    setUp = test_backend_profile_commands.BackendProfileCommandTests.setUp
    headers_for = test_backend_profile_commands.BackendProfileCommandTests.headers_for
    create = test_backend_profile_commands.BackendProfileCommandTests.create

    def prepare(self):
        profile = self.create().json()['profile']['id']
        with self.db.transaction() as conn:
            conn.execute('INSERT INTO backend_nodes(key, title, region, protocols_json) VALUES (?, ?, ?, ?)',
                         ('node', 'Node', 'test', json.dumps(['awg', 'xray'])))
        return profile

    def grants(self, profile, revision, grants, key=None):
        return self.client.patch(f'/api/v1/profiles/{profile}/grants',
            headers=self.headers_for(revision, key), json={'grants': grants})

    def operation(self, result):
        response = self.client.get('/api/v1/operations/' + result['operation_id'], headers=self.headers_for())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn('intent_json', response.text)
        self.assertNotIn('runtime_name', response.text)
        return response.json()

    def test_outbox_revokes_removed_and_historical_targets(self):
        profile = self.prepare()
        first = self.grants(profile, 1, [{'node_key': 'node', 'protocol': 'awg'}]).json()
        self.assertEqual(first['runtime_status'], 'awaiting_executor')
        self.assertEqual(self.operation(first)['tasks'][0]['action'], 'ensure')
        removed = self.grants(profile, 2, []).json()
        self.assertEqual(self.operation(removed)['tasks'][0]['action'], 'delete')
        edited = self.client.patch(f'/api/v1/profiles/{profile}', headers=self.headers_for(3),
                                  json={'frozen': True}).json()
        self.assertEqual(self.operation(edited)['tasks'][0]['action'], 'delete')

    def test_freeze_expiry_and_unfreeze_intents(self):
        profile = self.prepare()
        self.grants(profile, 1, [{'node_key': 'node', 'protocol': 'xray'}])
        for revision, values, action in [(2, {'frozen': True}, 'delete'),
                                         (3, {'frozen': False}, 'ensure'),
                                         (4, {'expires_at': '2000-01-01T00:00:00Z'}, 'delete')]:
            result = self.client.patch(f'/api/v1/profiles/{profile}', headers=self.headers_for(revision), json=values).json()
            self.assertEqual(self.operation(result)['tasks'][0]['action'], action)

    def test_replay_does_not_duplicate_operation_or_task(self):
        profile = self.prepare()
        key = str(uuid4())
        grants = [{'node_key': 'node', 'protocol': 'awg'}]
        first = self.grants(profile, 1, grants, key)
        second = self.grants(profile, 1, grants, key)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_operation_tasks').fetchone()[0], 1)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_operations').fetchone()[0], 2)

    def test_outbox_failure_rolls_back_desired_state_and_idempotency_record(self):
        profile = self.prepare()
        with self.db.transaction() as conn:
            conn.execute("""CREATE TRIGGER fail_outbox BEFORE INSERT ON backend_operation_tasks
                BEGIN SELECT RAISE(ABORT, 'private database detail'); END""")
        key = str(uuid4())
        result = self.grants(profile, 1, [{'node_key': 'node', 'protocol': 'awg'}], key)
        self.assertEqual(result.status_code, 500)
        self.assertNotIn('private database detail', result.text)
        self.assertEqual(self.db.connection.execute('SELECT desired_revision FROM backend_profiles WHERE id = ?', (profile,)).fetchone()[0], 1)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_grants').fetchone()[0], 0)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_profile_commands WHERE command_key = ?', (key,)).fetchone()[0], 0)

    def test_foreign_member_cannot_inspect_operation(self):
        created = self.create().json()
        member = self.identities.resolve_telegram(102)
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_accounts SET status = 'approved' WHERE id = ?", (member.id,))
        headers = {**self.headers_for(), 'X-Node-Plane-Telegram-User-ID': '102'}
        response = self.client.get('/api/v1/operations/' + created['operation_id'], headers=headers)
        self.assertEqual(response.status_code, 404)
