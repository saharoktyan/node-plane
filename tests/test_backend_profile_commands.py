from __future__ import annotations

from uuid import uuid4
import json
import unittest

from tests import test_backend_http


class BackendProfileCommandTests(unittest.TestCase):
    setUp = test_backend_http.BackendHTTPTests.setUp

    def headers_for(self, revision=None, key=None):
        headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101', 'Idempotency-Key': key or str(uuid4())}
        if revision is not None:
            headers['If-Match'] = f'"{revision}"'
        return headers

    def create(self, key=None, **fields):
        return self.client.post('/api/v1/profiles', headers=self.headers_for(key=key),
                                json={'display_name': 'Alice', 'owner_account_id': self.admin.id, **fields})

    def test_create_replay_and_conflict(self):
        key = str(uuid4())
        first = self.create(key)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(first.json()['runtime_status'], 'no_targets')
        self.assertEqual(first.headers['ETag'], '"1"')
        self.assertEqual(self.create(key).json(), first.json())
        self.assertEqual(self.create(key, display_name='Changed').status_code, 409)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_profiles').fetchone()[0], 1)

    def test_edit_requires_revision_and_preserves_runtime_identity(self):
        profile_id = self.create().json()['profile']['id']
        old_name = self.db.connection.execute('SELECT runtime_name FROM backend_profiles WHERE id = ?', (profile_id,)).fetchone()[0]
        path = f'/api/v1/profiles/{profile_id}'
        self.assertEqual(self.client.patch(path, headers=self.headers_for(), json={'display_name': 'New'}).status_code, 428)
        changed = self.client.patch(path, headers=self.headers_for(1), json={'display_name': 'New', 'frozen': True})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()['profile']['desired_revision'], 2)
        self.assertEqual(changed.headers['ETag'], '"2"')
        self.assertEqual(self.db.connection.execute('SELECT runtime_name FROM backend_profiles WHERE id = ?', (profile_id,)).fetchone()[0], old_name)
        self.assertEqual(self.client.patch(path, headers=self.headers_for(1), json={'frozen': False}).status_code, 412)

    def test_edit_replay_returns_original_result_without_second_change(self):
        profile_id = self.create().json()['profile']['id']
        path = f'/api/v1/profiles/{profile_id}'
        headers = self.headers_for(1)
        body = {'frozen': True}
        first = self.client.patch(path, headers=headers, json=body)
        replay = self.client.patch(path, headers=headers, json=body)
        self.assertEqual(first.json(), replay.json())
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(self.client.get(path, headers=self.headers_for()).headers['ETag'], '"2"')

    def test_edit_rejects_ownership_runtime_and_invalid_expiry(self):
        profile_id = self.create().json()['profile']['id']
        self.db.connection.execute('UPDATE backend_profiles SET owner_account_id = NULL WHERE id = ?', (profile_id,))
        self.db.connection.commit()
        path = f'/api/v1/profiles/{profile_id}'
        for body in ({'owner_account_id': str(uuid4())}, {'runtime_name': 'another'}, {'frozen': 'true'},
                     {'expires_at': '2026-01-01T00:00:00'}, {'display_name': '   '}, {}, {'display_name': None}):
            self.assertEqual(self.client.patch(path, headers=self.headers_for(1), json=body).status_code, 422, body)
        result = self.client.patch(path, headers=self.headers_for(1), json={'expires_at': '2027-01-01T02:00:00+02:00'})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['profile']['expires_at'], '2027-01-01T00:00:00+00:00')

    def test_member_cannot_mutate_even_with_scopes(self):
        self.identities.resolve_telegram(102)
        self.db.connection.execute("UPDATE backend_accounts SET status = 'approved' WHERE id != ?", (self.admin.id,))
        headers = {**self.headers_for(), 'X-Node-Plane-Telegram-User-ID': '102'}
        response = self.client.post('/api/v1/profiles', headers=headers, json={'display_name': 'Other'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_profiles').fetchone()[0], 0)

    def test_grants_replace_atomically_and_validate_target(self):
        profile_id = self.create().json()['profile']['id']
        self.db.connection.execute('INSERT INTO backend_nodes(key, title, region, protocols_json) VALUES (?, ?, ?, ?)',
                                   ('node', 'Node', 'test', json.dumps(['awg', 'xray'])))
        path = f'/api/v1/profiles/{profile_id}/grants'
        response = self.client.patch(path, headers=self.headers_for(1), json={'grants': [{'node_key': 'node', 'protocol': 'awg'}]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['profile']['desired_revision'], 2)
        invalid = self.client.patch(path, headers=self.headers_for(2), json={'grants': [{'node_key': 'missing', 'protocol': 'xray'}]})
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(self.db.connection.execute('SELECT protocol FROM backend_grants').fetchone()[0], 'awg')
        cleared = self.client.patch(path, headers=self.headers_for(2), json={'grants': []})
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.json()['grants'], [])

    def test_replay_rechecks_current_permissions(self):
        headers = self.headers_for()
        body = {'display_name': 'Alice'}
        response = self.client.post('/api/v1/profiles', headers=headers, json=body)
        self.assertEqual(response.status_code, 201)
        self.db.connection.execute("UPDATE backend_accounts SET role = 'member' WHERE id = ?", (self.admin.id,))
        self.assertEqual(self.client.post('/api/v1/profiles', headers=headers, json=body).status_code, 403)

    def test_invalid_owner_and_grant_duplicates_do_not_change_state(self):
        self.assertEqual(self.create(owner_account_id=str(uuid4())).status_code, 422)
        profile_id = self.create().json()['profile']['id']
        self.db.connection.execute('INSERT INTO backend_nodes(key, title, region, protocols_json) VALUES (?, ?, ?, ?)', ('n', 'N', 'test', '["awg"]'))
        grant = {'node_key': 'n', 'protocol': 'awg'}
        response = self.client.patch(f'/api/v1/profiles/{profile_id}/grants', headers=self.headers_for(1), json={'grants': [grant, grant]})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.db.connection.execute('SELECT desired_revision FROM backend_profiles').fetchone()[0], 1)

    def test_deleting_last_member_profile_revokes_menu_and_allows_fresh_approval(self):
        from backend.profiles import ProfileRepository
        member = self.identities.resolve_telegram(102)
        self.db.connection.execute("UPDATE backend_accounts SET status = 'approved' WHERE id = ?", (member.id,))
        repo = ProfileRepository(self.db)
        old_id = repo.ensure_account_profile(member.id)
        old_runtime = self.db.connection.execute('SELECT runtime_name FROM backend_profiles WHERE id = ?', (old_id,)).fetchone()[0]
        headers = self.headers_for(1)
        path = f'/api/v1/profiles/{old_id}'
        first = self.client.delete(path, headers=headers)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(self.client.delete(path, headers=headers).json(), first.json())
        member_headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '102'}
        me = self.client.get('/api/v1/me', headers=member_headers).json()
        self.assertEqual(me['status'], 'pending')
        self.assertNotIn('profiles.self.read', me['permissions'])
        self.assertEqual(self.client.get('/api/v1/me/profiles', headers=member_headers).status_code, 403)
        request = self.client.post('/api/v1/me/access-requests',
            headers={**member_headers, 'Idempotency-Key': str(uuid4())}).json()
        approved = self.client.post(f"/api/v1/access-requests/{request['id']}/decision",
            headers=self.headers_for(), json={'decision': 'approve'})
        self.assertEqual(approved.status_code, 200, approved.text)
        new_id = repo.ensure_account_profile(member.id)
        self.assertNotEqual(new_id, old_id)
        self.assertEqual(repo.ensure_account_profile(member.id), new_id)
        new_runtime = self.db.connection.execute('SELECT runtime_name FROM backend_profiles WHERE id = ?', (new_id,)).fetchone()[0]
        self.assertNotEqual(new_runtime, old_runtime)
        self.assertEqual(self.client.get('/api/v1/me', headers=member_headers).json()['status'], 'approved')
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_grants WHERE profile_id = ?', (new_id,)).fetchone()[0], 0)

    def test_deletion_preserves_admin_and_member_with_another_profile(self):
        admin_profile = self.create().json()['profile']['id']
        self.assertEqual(self.client.delete(f'/api/v1/profiles/{admin_profile}', headers=self.headers_for(1)).status_code, 200)
        self.assertEqual(self.identities.get_account(self.admin.id).status, 'approved')
        member = self.identities.resolve_telegram(102)
        self.db.connection.execute("UPDATE backend_accounts SET status = 'approved' WHERE id = ?", (member.id,))
        one = self.create(owner_account_id=member.id).json()['profile']['id']
        two = self.create(owner_account_id=member.id).json()['profile']['id']
        self.assertEqual(self.client.delete(f'/api/v1/profiles/{one}', headers=self.headers_for(1)).status_code, 200)
        self.assertEqual(self.identities.get_account(member.id).status, 'approved')
        self.assertEqual(self.client.delete(f'/api/v1/profiles/{two}', headers=self.headers_for(1)).status_code, 200)
        self.assertEqual(self.identities.get_account(member.id).status, 'pending')

    def test_schema_repairs_legacy_orphan_approval_once(self):
        from backend.profiles import ProfileRepository
        member = self.identities.resolve_telegram(102)
        profile_id = self.create(owner_account_id=member.id).json()['profile']['id']
        self.db.connection.execute("INSERT INTO backend_profile_deletions(profile_id, requested_at) VALUES (?, 'legacy')", (profile_id,))
        self.db.connection.execute("UPDATE backend_accounts SET status = 'approved' WHERE id = ?", (member.id,))
        ProfileRepository(self.db).initialize_schema()
        self.assertEqual(self.identities.get_account(member.id).status, 'pending')
        revision = self.db.connection.execute('SELECT revision FROM backend_accounts WHERE id = ?', (member.id,)).fetchone()[0]
        ProfileRepository(self.db).initialize_schema()
        self.assertEqual(self.db.connection.execute('SELECT revision FROM backend_accounts WHERE id = ?', (member.id,)).fetchone()[0], revision)
        self.assertEqual(self.identities.get_account(self.admin.id).status, 'approved')


    def test_admin_duration_is_permanent_and_combined_edit_is_atomic(self):
        blocked = self.create(expires_at='2099-01-01T00:00:00Z')
        self.assertEqual(blocked.status_code, 422)
        self.assertEqual(blocked.json()['error']['code'], 'admin_expiry_forbidden')
        profile = self.create().json()['profile']['id']
        path = f'/api/v1/profiles/{profile}'
        blocked = self.client.patch(path, headers=self.headers_for(1), json={'expires_at': '2099-01-01T00:00:00Z'})
        self.assertEqual(blocked.status_code, 422)
        self.db.connection.execute('UPDATE backend_profiles SET owner_account_id = NULL WHERE id = ?', (profile,))
        key = str(uuid4())
        changed = self.client.patch(path, headers=self.headers_for(1, key),
            json={'expires_at': '2099-01-01T00:00:00Z', 'grants': []})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()['profile']['desired_revision'], 2)
        replay = self.client.patch(path, headers=self.headers_for(1, key),
            json={'expires_at': '2099-01-01T00:00:00Z', 'grants': []})
        self.assertEqual(changed.json(), replay.json())
        failed = self.client.patch(path, headers=self.headers_for(2), json={'expires_at': None,
            'grants': [{'node_key': 'missing', 'protocol': 'awg'}]})
        self.assertEqual(failed.status_code, 422)
        current = self.client.get(path, headers=self.headers_for()).json()
        self.assertEqual(current['expires_at'], '2099-01-01T00:00:00+00:00')
        self.assertEqual(current['desired_revision'], 2)
