from __future__ import annotations

from uuid import uuid4
import unittest

from tests.test_backend_http import BackendHTTPTests


class BackendAccountTests(unittest.TestCase):
    setUp = BackendHTTPTests.setUp
    register = BackendHTTPTests.register

    def headers_for(self, telegram_id):
        return {**self.headers, 'X-Node-Plane-Telegram-User-ID': str(telegram_id)}

    def patch_account(self, account_id, revision, body, *, key=None, telegram_id=101):
        return self.client.patch(f'/api/v1/accounts/{account_id}',
            headers={**self.headers_for(telegram_id), 'If-Match': f'"{revision}"',
                     'Idempotency-Key': key or str(uuid4())}, json=body)

    def test_admin_account_lifecycle_and_idempotency(self):
        member = self.register(102).json()
        self.assertEqual(self.client.get('/api/v1/accounts', headers=self.headers_for(102)).status_code, 403)
        listed = self.client.get('/api/v1/accounts', headers=self.headers_for(101))
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual({item['id'] for item in listed.json()['items']}, {self.admin.id, member['id']})
        detail = self.client.get(f"/api/v1/accounts/{member['id']}", headers=self.headers_for(101))
        self.assertEqual(detail.json()['telegram_user_id'], 102)
        revision = detail.json()['revision']
        self.assertEqual(detail.headers['ETag'], f'"{revision}"')
        key = str(uuid4())
        approved = self.patch_account(member['id'], revision, {'status': 'approved'}, key=key)
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()['revision'], revision + 1)
        self.assertEqual(self.patch_account(member['id'], revision, {'status': 'approved'}, key=key).json(), approved.json())
        self.assertEqual(self.patch_account(member['id'], revision, {'role': 'admin'}, key=key).json()['error']['code'], 'idempotency_conflict')
        self.assertEqual(self.patch_account(member['id'], revision, {'role': 'admin'}).status_code, 412)
        promoted = self.patch_account(member['id'], revision + 1, {'role': 'admin'})
        self.assertEqual(promoted.status_code, 200, promoted.text)
        self.assertEqual(self.client.get('/api/v1/me', headers=self.headers_for(102)).json()['role'], 'admin')
        self.assertEqual(self.patch_account(self.admin.id, 2, {'status': 'disabled'}).json()['error']['code'], 'self_admin_revocation')

    def test_last_admin_and_pending_request_protections(self):
        member = self.register(102).json()
        own = self.client.get(f'/api/v1/accounts/{self.admin.id}', headers=self.headers_for(101)).json()
        self.assertEqual(self.patch_account(self.admin.id, own['revision'], {'role': 'member'}).json()['error']['code'], 'self_admin_revocation')
        self.client.post('/api/v1/me/access-requests', headers={**self.headers_for(102),
            'Idempotency-Key': str(uuid4())})
        result = self.patch_account(member['id'], 1, {'status': 'approved'})
        self.assertEqual(result.json()['error']['code'], 'request_pending')
        request_id = self.client.get('/api/v1/access-requests', headers=self.headers_for(101)).json()['items'][0]['id']
        self.client.post(f'/api/v1/access-requests/{request_id}/decision',
            headers={**self.headers_for(101), 'Idempotency-Key': str(uuid4())}, json={'decision': 'approve'})
        self.assertEqual(self.patch_account(member['id'], 1, {'role': 'admin'}).status_code, 412)
        self.assertEqual(self.patch_account(member['id'], 2, {'role': 'admin'}).status_code, 200)
        member_admin_headers = self.headers_for(102)
        revoked = self.client.patch(f'/api/v1/accounts/{self.admin.id}',
            headers={**member_admin_headers, 'If-Match': f'"{own["revision"]}"',
                     'Idempotency-Key': str(uuid4())}, json={'status': 'disabled'})
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertEqual(self.client.get('/api/v1/accounts', headers=self.headers_for(101)).status_code, 403)
        remaining = self.client.get(f"/api/v1/accounts/{member['id']}", headers=member_admin_headers).json()
        self.assertEqual(self.patch_account(member['id'], remaining['revision'], {'status': 'disabled'}, telegram_id=102).json()['error']['code'], 'self_admin_revocation')

    def test_member_cannot_change_account_and_bad_headers_fail(self):
        member = self.register(102).json()
        self.assertEqual(self.patch_account(member['id'], 1, {'status': 'approved'}, telegram_id=102).status_code, 403)
        self.assertEqual(self.client.patch(f"/api/v1/accounts/{member['id']}",
            headers={**self.headers_for(101), 'Idempotency-Key': str(uuid4())},
            json={'status': 'approved'}).status_code, 428)
        self.assertEqual(self.patch_account(member['id'], 1, {}).status_code, 422)
        self.assertEqual(self.client.get('/api/v1/me', headers=self.headers_for(102)).json()['status'], 'pending')


    def test_promotion_removes_profile_expiry_in_same_transaction(self):
        member = self.register(102).json()
        account = self.client.get(f"/api/v1/accounts/{member['id']}", headers=self.headers_for(101)).json()
        approved = self.patch_account(member['id'], account['revision'], {'status': 'approved'}).json()
        created = self.client.post('/api/v1/profiles', headers={**self.headers_for(101), 'Idempotency-Key': str(uuid4())},
            json={'display_name': 'Member', 'owner_account_id': member['id'], 'expires_at': '2099-01-01T00:00:00Z'})
        self.assertEqual(created.status_code, 201, created.text)
        profile = created.json()['profile']['id']
        promoted = self.patch_account(member['id'], approved['revision'], {'role': 'admin'})
        self.assertEqual(promoted.status_code, 200, promoted.text)
        row = self.db.connection.execute('SELECT expires_at, desired_revision FROM backend_profiles WHERE id = ?', (profile,)).fetchone()
        self.assertIsNone(row['expires_at'])
        self.assertEqual(row['desired_revision'], 2)
