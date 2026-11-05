from __future__ import annotations

from uuid import uuid4
import unittest

from tests.test_backend_http import BackendHTTPTests


class BackendAccountTests(unittest.TestCase):
    setUp = BackendHTTPTests.setUp
    register = BackendHTTPTests.register
    provision = BackendHTTPTests.provision

    def headers_for(self, telegram_id):
        return {**self.headers, 'X-Node-Plane-Telegram-User-ID': str(telegram_id)}

    def patch_account(self, account_id, revision, body, *, key=None, telegram_id=101):
        return self.client.patch(f'/api/v1/accounts/{account_id}',
            headers={**self.headers_for(telegram_id), 'If-Match': f'"{revision}"',
                     'Idempotency-Key': key or str(uuid4())}, json=body)

    def test_menu_refresh_ack_cannot_drop_a_newer_role_change(self):
        member = self.provision(102).json()['account']
        path = '/api/v1/integrations/telegram/menu-refresh'
        first = self.client.post(path + '/claim', headers=self.headers).json()['delivery']
        self.assertEqual(first['account']['telegram_user_id'], 102)
        promoted = self.patch_account(member['id'], member['revision'], {'role': 'admin'})
        self.assertEqual(promoted.status_code, 200, promoted.text)
        self.client.post(f"{path}/{member['id']}/ack?revision={first['revision']}", headers=self.headers)
        current = self.client.post(path + '/claim', headers=self.headers).json()['delivery']
        self.assertEqual(current['account']['role'], 'admin')
        self.assertGreater(current['revision'], first['revision'])
        self.client.post(f"{path}/{member['id']}/ack?revision={current['revision']}", headers=self.headers)
        self.assertIsNone(self.client.post(path + '/claim', headers=self.headers).json()['delivery'])
        self.assertEqual(self.client.post(path + '/claim', headers=self.headers_for(101)).status_code, 422)

    def test_person_without_telegram_is_provisioned_by_name(self):
        from backend.workstation_cli import SCOPES
        from backend.authorization import PrincipalKind
        _, token = self.credentials.issue(PrincipalKind.ACCOUNT, SCOPES, account_id=self.admin.id)
        def post(body, key):
            return self.client.post('/api/v1/accounts', json=body,
                headers={'Authorization': 'Bearer ' + token, 'Idempotency-Key': key})
        key = str(uuid4())
        created = post({'display_name': ' Ann Lee '}, key)
        self.assertEqual(created.status_code, 201, created.text)
        value = created.json()
        self.assertEqual((value['account']['display_name'], value['account']['telegram_user_id'],
                          value['account']['status'], value['profile']['display_name'], value['created']),
                         ('Ann Lee', None, 'approved', 'Ann Lee', True))
        self.assertEqual(post({'display_name': 'Ann Lee'}, key).json()['account']['id'], value['account']['id'])
        self.assertEqual(post({'display_name': 'Bo'}, key).status_code, 409)
        self.assertIsNone(self.db.connection.execute('SELECT 1 FROM backend_account_channels WHERE account_id=?',
                                                     (value['account']['id'],)).fetchone())
        for body in ({}, {'identity': '102', 'display_name': 'Ann'}, {'display_name': 'a\nb'}):
            with self.subTest(body=body):
                self.assertEqual(post(body, str(uuid4())).status_code, 422)

    def test_admin_account_lifecycle_and_idempotency(self):
        guest = self.register(102).json()
        self.assertEqual(guest['status'], 'pending')
        self.assertEqual(self.client.get('/api/v1/accounts', headers=self.headers_for(102)).status_code, 401)
        member = self.provision(102).json()['account']
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
        self.assertEqual(approved.json()['revision'], revision)
        self.assertEqual(self.patch_account(member['id'], revision, {'status': 'approved'}, key=key).json(), approved.json())
        self.assertEqual(self.patch_account(member['id'], revision, {'role': 'admin'}, key=key).json()['error']['code'], 'idempotency_conflict')
        self.assertEqual(self.patch_account(member['id'], revision + 1, {'role': 'admin'}).status_code, 412)
        promoted = self.patch_account(member['id'], revision, {'role': 'admin'})
        self.assertEqual(promoted.status_code, 200, promoted.text)
        self.assertEqual(self.client.get('/api/v1/me', headers=self.headers_for(102)).json()['role'], 'admin')
        self.assertEqual(self.patch_account(self.admin.id, 2, {'status': 'disabled'}).json()['error']['code'], 'self_admin_revocation')

    def test_last_admin_and_pending_request_protections(self):
        member_id = str(uuid4())
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_accounts (id, role, status) VALUES (?, 'member', 'approved')", (member_id,))
            conn.execute("INSERT INTO backend_external_identities (provider, subject, account_id) VALUES ('telegram', '102', ?)", (member_id,))
        own = self.client.get(f'/api/v1/accounts/{self.admin.id}', headers=self.headers_for(101)).json()
        self.assertEqual(self.patch_account(self.admin.id, own['revision'], {'role': 'member'}).json()['error']['code'], 'self_admin_revocation')
        self.client.post('/api/v1/me/access-requests', headers={**self.headers_for(102),
            'Idempotency-Key': str(uuid4())})
        result = self.patch_account(member_id, 1, {'role': 'admin'})
        self.assertEqual(result.json()['error']['code'], 'request_pending')
        request_id = self.client.get('/api/v1/access-requests', headers=self.headers_for(101)).json()['items'][0]['id']
        self.client.post(f'/api/v1/access-requests/{request_id}/decision',
            headers={**self.headers_for(101), 'Idempotency-Key': str(uuid4())}, json={'decision': 'approve'})
        self.assertEqual(self.patch_account(member_id, 1, {'role': 'admin'}).status_code, 412)
        self.assertEqual(self.patch_account(member_id, 2, {'role': 'admin'}).status_code, 200)
        member_admin_headers = self.headers_for(102)
        revoked = self.client.patch(f'/api/v1/accounts/{self.admin.id}',
            headers={**member_admin_headers, 'If-Match': f'"{own["revision"]}"',
                     'Idempotency-Key': str(uuid4())}, json={'status': 'disabled'})
        self.assertEqual(revoked.status_code, 409, revoked.text)
        self.assertEqual(revoked.json()['error']['code'], 'admin_block_forbidden')
        # An administrator does not demote an owner; another owner does.
        demoted = self.client.patch(f'/api/v1/accounts/{self.admin.id}',
            headers={**member_admin_headers, 'If-Match': f'"{own["revision"]}"', 'Idempotency-Key': str(uuid4())}, json={'role':'member'})
        self.assertEqual(demoted.status_code, 403, demoted.text)
        with self.db.transaction() as conn:
            from backend.roles import assign
            assign(conn, member_id, {'owner'})
        demoted = self.client.patch(f'/api/v1/accounts/{self.admin.id}',
            headers={**member_admin_headers, 'If-Match': f'"{own["revision"]}"', 'Idempotency-Key': str(uuid4())}, json={'role':'member'})
        self.assertEqual(demoted.status_code, 200, demoted.text)
        self.assertEqual(self.client.get('/api/v1/accounts', headers=self.headers_for(101)).status_code, 403)
        remaining = self.client.get(f"/api/v1/accounts/{member_id}", headers=member_admin_headers).json()
        self.assertEqual(self.patch_account(member_id, remaining['revision'], {'status': 'disabled'}, telegram_id=102).json()['error']['code'], 'self_admin_revocation')

    def test_member_cannot_change_account_and_bad_headers_fail(self):
        self.register(102)
        self.assertEqual(self.patch_account(self.admin.id, 1, {'status': 'approved'}, telegram_id=102).status_code, 401)
        member = self.provision(102).json()['account']
        self.assertEqual(self.patch_account(member['id'], 1, {'status': 'approved'}, telegram_id=102).status_code, 403)
        self.assertEqual(self.client.patch(f"/api/v1/accounts/{member['id']}",
            headers={**self.headers_for(101), 'Idempotency-Key': str(uuid4())},
            json={'status': 'approved'}).status_code, 428)
        self.assertEqual(self.patch_account(member['id'], 1, {}).status_code, 422)
        self.assertEqual(self.client.get('/api/v1/me', headers=self.headers_for(102)).json()['status'], 'approved')

    def test_promotion_removes_profile_expiry_in_same_transaction(self):
        member = self.provision(102).json()['account']
        account = self.client.get(f"/api/v1/accounts/{member['id']}", headers=self.headers_for(101)).json()
        profile = self.db.connection.execute('SELECT id FROM backend_profiles WHERE owner_account_id=?',(member['id'],)).fetchone()[0]
        changed = self.client.patch(f'/api/v1/profiles/{profile}', headers={**self.headers_for(101), 'If-Match':'"1"', 'Idempotency-Key':str(uuid4())},json={'expires_at':'2099-01-01T00:00:00Z'})
        self.assertEqual(changed.status_code,200,changed.text)
        promoted = self.patch_account(member['id'], account['revision'], {'role': 'admin'})
        self.assertEqual(promoted.status_code, 200, promoted.text)
        row = self.db.connection.execute('SELECT expires_at, desired_revision FROM backend_profiles WHERE id = ?', (profile,)).fetchone()
        self.assertIsNone(row['expires_at'])
        self.assertEqual(row['desired_revision'], 3)

    def test_accounts_are_only_active_or_disabled(self):
        member = self.provision(102).json()['account']
        for status in ('pending', 'rejected'):
            self.assertEqual(self.patch_account(member['id'], member['revision'], {'status': status}).status_code, 422)
        self.assertEqual(self.patch_account(member['id'], member['revision'], {'status': 'disabled'}).status_code, 200)


    def test_an_owner_assigns_exact_roles_and_keeps_one_owner(self):
        member = self.provision(102).json()['account']
        made = self.patch_account(member['id'], member['revision'], {'roles': ['operator', 'support']})
        self.assertEqual(made.status_code, 200, made.text)
        self.assertEqual((made.json()['role'], made.json()['roles']), ('admin', ['operator', 'support']))
        me = self.client.get('/api/v1/me', headers=self.headers_for(102)).json()
        self.assertIn('nodes.manage', me['permissions'])
        self.assertIn('access_requests.manage', me['permissions'])
        self.assertNotIn('accounts.manage', me['permissions'])
        changed = self.patch_account(member['id'], made.json()['revision'], {'roles': ['support']})
        self.assertEqual(changed.json()['roles'], ['support'])
        self.assertNotIn('nodes.manage', self.client.get('/api/v1/me', headers=self.headers_for(102)).json()['permissions'])
        # The only owner cannot give the role up; a second owner can take it away.
        own = self.client.get(f'/api/v1/accounts/{self.admin.id}', headers=self.headers_for(101)).json()
        self.assertEqual(own['roles'], ['owner'])
        kept = self.patch_account(self.admin.id, own['revision'], {'roles': ['admin']})
        self.assertEqual(kept.json()['error']['code'], 'last_owner_required')
        self.patch_account(member['id'], changed.json()['revision'], {'roles': ['owner']})
        self.assertEqual(self.patch_account(self.admin.id, own['revision'], {'roles': ['admin']}).json()['roles'], ['admin'])
        # An administrator assigns no roles.
        other = self.provision(103).json()['account']
        refused = self.patch_account(other['id'], other['revision'], {'roles': ['operator']})
        self.assertEqual(refused.status_code, 403, refused.text)
        self.assertEqual(self.patch_account(other['id'], other['revision'], {'roles': ['root']}, telegram_id=102).status_code, 422)
