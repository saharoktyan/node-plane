import unittest
from uuid import uuid4

from tests import test_backend_nodes as fixture


class GrantPolicyHTTPTests(unittest.TestCase):
    setUp = fixture.BackendNodeTests.setUp
    admin_headers = fixture.BackendNodeTests.admin_headers
    create = fixture.BackendNodeTests.create

    def profile(self):
        response = self.client.post('/api/v1/profiles', headers=self.admin_headers(**{'Idempotency-Key': str(uuid4())}),
                                    json={'display_name': 'Alice'})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()['profile']['id']

    def test_wizard_saves_policy_and_expiry_atomically(self):
        node = self.create().json()
        self.db.connection.execute("UPDATE backend_nodes SET enabled=1,applied_revision=1 WHERE key='lv1'")
        policy = {'explicit_grants': [], 'rules': [{'scope': 'region', 'region_id': node['region_id'], 'protocols': ['awg']}], 'exclusions': []}
        response = self.client.post('/api/v1/profiles', headers=self.admin_headers(**{'Idempotency-Key': str(uuid4())}),
                                    json={'display_name': 'Alice', 'expires_at': '2099-01-01T00:00:00Z', 'access_policy': policy})
        self.assertEqual(response.status_code, 201, response.text)
        profile = response.json()['profile']['id']
        path = f'/api/v1/profiles/{profile}'
        stored = self.client.get(path + '/access-policy', headers=self.admin_headers()).json()
        self.assertEqual(stored['rules'], policy['rules'])
        self.assertEqual(stored['inherited_grants'], [{'node_key': 'lv1', 'protocol': 'awg'}])
        headers = self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"1"'})
        changed = {**policy, 'rules': [{'scope': 'all', 'region_id': None, 'protocols': ['xray']}]}
        response = self.client.patch(path, headers=headers, json={'expires_at': None, 'access_policy': changed})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['profile']['desired_revision'], 2)
        self.assertIsNone(response.json()['profile']['expires_at'])
        self.assertEqual(self.client.patch(path, headers=headers, json={'expires_at': None, 'access_policy': changed}).json(), response.json())
        before = self.client.get(path + '/access-policy', headers=self.admin_headers()).json()
        for invalid in ({'display_name': '   ', 'access_policy': policy}, {'grants': [], 'access_policy': policy}):
            result = self.client.patch(path, headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"2"'}), json=invalid)
            self.assertEqual(result.status_code, 422, result.text)
            self.assertEqual(self.client.get(path + '/access-policy', headers=self.admin_headers()).json(), before)

    def test_revision_permission_and_preview_contract(self):
        node = self.create().json()
        self.db.connection.execute("UPDATE backend_nodes SET enabled=1,applied_revision=1 WHERE key='lv1'")
        profile = self.profile()
        body = {'explicit_grants': [], 'rules': [{'scope': 'region', 'region_id': node['region_id'], 'protocols': ['awg']}], 'exclusions': []}
        path = f'/api/v1/profiles/{profile}/access-policy'
        headers = self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"1"'})
        result = self.client.put(path, headers=headers, json=body)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.headers['ETag'], '"2"')
        self.assertEqual(self.client.put(path, headers=headers, json=body).json(), result.json())
        response = self.client.get(path, headers=self.admin_headers())
        self.assertEqual(response.json()['inherited_grants'], [{'node_key': 'lv1', 'protocol': 'awg'}])
        self.assertEqual(response.json()['explicit_grants'], [])
        self.assertEqual(response.headers['ETag'], '"2"')
        preview = self.client.get('/api/v1/nodes/lv1/region-access-preview', params={'region': 'Asia'}, headers=self.admin_headers())
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()['affected_profiles'], 1)
        self.assertEqual(preview.json()['profiles'][0]['removed_protocols'], ['awg'])
        self.assertEqual(self.client.get('/api/v1/regions', headers=self.admin_headers()).json()['items'][0]['id'], node['region_id'])
        stale = self.client.put(path, headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"1"'}), json=body)
        self.assertEqual(stale.status_code, 412)
        missing_revision = self.client.put(path, headers=self.admin_headers(**{'Idempotency-Key': str(uuid4())}), json=body)
        self.assertEqual(missing_revision.status_code, 428)
        account = self.identities.resolve_telegram(102)
        self.db.connection.execute("UPDATE backend_accounts SET status='approved' WHERE id=?", (account.id,))
        member = {**self.admin_headers(), 'X-Node-Plane-Telegram-User-ID': '102'}
        self.assertEqual(self.client.get(path, headers=member).status_code, 403)
        self.assertEqual(self.client.put(path, headers={**member, 'Idempotency-Key': str(uuid4()), 'If-Match': '"2"'}, json=body).status_code, 403)

    def test_all_rule_can_precede_any_server_and_invalid_input_never_mutates(self):
        profile = self.profile()
        path = f'/api/v1/profiles/{profile}/access-policy'
        body = {'explicit_grants': [], 'rules': [{'scope': 'all', 'region_id': None, 'protocols': ['awg', 'xray']}], 'exclusions': []}
        headers = self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"1"'})
        response = self.client.put(path, headers=headers, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['grants'], [])
        bad = {**body, 'rules': [{'scope': 'all', 'region_id': str(uuid4()), 'protocols': ['awg']}]}
        response = self.client.put(path, headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"2"'}), json=bad)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client.get(path, headers=self.admin_headers()).json()['revision'], 2)
