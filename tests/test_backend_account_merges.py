"""Merging accounts by request: codes, the preview, a VPN profile that moves or is chosen, refusals, the API."""
from datetime import datetime, timezone
from uuid import uuid4
import unittest

from backend import account_merges as merges
from backend import logins, sessions
from backend.authorization import AccessDenied, Principal, PrincipalKind, resolve_actor
from backend.credentials import PERMISSIONS
from tests import test_backend_http as http_fixture

NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)


class MergeTests(unittest.TestCase):
    setUp = http_fixture.BackendHTTPTests.setUp
    register = http_fixture.BackendHTTPTests.register
    provision = http_fixture.BackendHTTPTests.provision

    def admin_actor(self):
        return resolve_actor(Principal('test', PrincipalKind.ACCOUNT, PERMISSIONS, account_id=self.admin.id),
                             self.identities)

    def code(self, call, *args, **kwargs):
        with self.assertRaises(AccessDenied) as caught:
            call(*args, **kwargs)
        return caught.exception.code

    def cabinet(self, login, password=None):
        account_id = str(uuid4())
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_accounts(id, status) VALUES (?, 'approved')", (account_id,))
            logins.claim(conn, account_id, login, 'first', NOW)
            if password:
                sessions.set_password(conn, account_id, password, now=NOW)
        return account_id

    def telegram_member(self, user_id):
        self.register(user_id, username=f'user{user_id}')
        return self.provision(user_id).json()['account']['id']

    def ask(self, main, other):
        value = merges.code(self.db, other, now=NOW)['code']
        return merges.request(self.db, main, value, source='s', now=NOW)

    def profile_owner(self, profile_id):
        with self.db.connect() as conn:
            return conn.execute('SELECT owner_account_id FROM backend_profiles WHERE id=?',
                                (profile_id,)).fetchone()['owner_account_id']

    def deleted(self, account_id):
        with self.db.connect() as conn:
            return conn.execute('SELECT completed_at FROM backend_account_deletions WHERE account_id=?',
                                (account_id,)).fetchone()

    def test_a_telegram_accounts_vpn_and_telegram_move_into_the_cabinet_account(self):
        other = self.telegram_member(202)
        main = self.cabinet('nora', 'correct horse battery')
        request = self.ask(main, other)
        preview = merges.get(self.db, self.admin_actor(), request['id'])['preview']
        self.assertEqual((preview['main']['login'], preview['other']['telegram_user_id']), ('nora', 202))
        self.assertEqual(preview['result'], {'vpn': 'moves', 'telegram': 'moves', 'password': None, 'login': None,
                                             'passkeys_removed': 0, 'keep_main_only': False})
        profile = preview['other']['vpn']['id']
        decided = merges.decide(self.db, self.admin_actor(), request['id'], 'approve', now=NOW)
        self.assertEqual(decided['status'], 'approved')
        self.assertEqual(self.profile_owner(profile), main)
        self.assertEqual(self.identities.find_telegram_account(202).id, main)
        self.assertIsNotNone(self.deleted(other)['completed_at'])
        # What the administrator saw stays with the request.
        self.assertEqual(merges.get(self.db, self.admin_actor(), request['id'])['preview']['other']['id'], other)

    def test_both_with_vpn_the_administrator_chooses(self):
        other = self.telegram_member(303)
        main = self.telegram_member(304)
        request = self.ask(main, other)
        preview = merges.get(self.db, self.admin_actor(), request['id'])['preview']
        self.assertEqual((preview['result']['vpn'], preview['result']['telegram']), ('choose', 'dropped'))
        self.assertEqual(self.code(merges.decide, self.db, self.admin_actor(), request['id'], 'approve'),
                         'merge_profile_choice_required')
        kept, dropped = preview['other']['vpn']['id'], preview['main']['vpn']['id']
        merges.decide(self.db, self.admin_actor(), request['id'], 'approve', keep_profile='other', now=NOW)
        self.assertEqual((self.profile_owner(kept), self.profile_owner(dropped)), (main, other))
        with self.db.connect() as conn:
            self.assertIsNotNone(conn.execute('SELECT 1 FROM backend_profile_deletions WHERE profile_id=?',
                                              (dropped,)).fetchone())
        # The other account goes with the dropped profile once it is revoked.
        self.assertIsNotNone(self.deleted(other))
        self.assertEqual(self.identities.find_telegram_account(304).id, main)

    def test_codes_and_refusals(self):
        main, other = self.cabinet('olga'), self.cabinet('pavel')
        self.assertEqual(self.code(merges.request, self.db, main, 'ABCD-EFGH', source='s', now=NOW),
                         'merge_code_invalid')
        value = merges.code(self.db, main, now=NOW)['code']
        self.assertEqual(self.code(merges.request, self.db, main, value, source='s', now=NOW), 'merge_same_account')
        self.assertEqual(self.code(merges.code, self.db, self.admin.id), 'merge_staff_account')
        request = self.ask(main, other)
        self.assertEqual(self.code(merges.code, self.db, other), 'merge_already_pending')
        # Nobody decides their own merge.
        main_actor = resolve_actor(Principal('t', PrincipalKind.ACCOUNT, PERMISSIONS, account_id=main), self.identities)
        self.assertIn(self.code(merges.decide, self.db, main_actor, request['id'], 'approve'),
                      {'merge_own_request', 'permission_denied'})
        rejected = merges.decide(self.db, self.admin_actor(), request['id'], 'reject', now=NOW)
        self.assertEqual(rejected['status'], 'rejected')
        self.assertIsNone(self.deleted(other))
        request = self.ask(main, other)
        merges.cancel(self.db, other, request['id'], now=NOW)
        self.assertEqual({item['status'] for item in merges.own(self.db, main)}, {'rejected', 'cancelled'})
        # A login and a password move to an account without them.
        with self.db.transaction() as conn:
            logins.release(conn, main, NOW)
            sessions.set_password(conn, other, 'correct horse battery', now=NOW)
        request = self.ask(main, other)
        merges.decide(self.db, self.admin_actor(), request['id'], 'approve', now=NOW)
        sessions.sign_in(self.db, 'pavel', 'correct horse battery', device='x', source='s', now=NOW)
        with self.db.connect() as conn:
            self.assertEqual(logins.current(conn, main), 'pavel')

    def test_api(self):
        from backend.workstation_cli import SCOPES
        other = self.telegram_member(505)
        main = self.cabinet('quinn')
        bot = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '505'}
        value = self.client.post('/api/v1/me/merge-code', headers=bot)
        self.assertEqual(value.status_code, 201, value.text)
        _, token = self.credentials.issue(PrincipalKind.ACCOUNT, frozenset({'account.self.read',
                                          'account.self.preferences.write'}), account_id=main)
        own = {'Authorization': 'Bearer ' + token}
        created = self.client.post('/api/v1/me/merge-requests', headers=own, json={'code': value.json()['code']})
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(self.client.get('/api/v1/me/merge-requests', headers=bot).json()['items'][0]['status'],
                         'pending')
        _, admin_token = self.credentials.issue(PrincipalKind.ACCOUNT, SCOPES, account_id=self.admin.id)
        admin = {'Authorization': 'Bearer ' + admin_token}
        listed = self.client.get('/api/v1/merge-requests', headers=admin).json()['items']
        self.assertEqual(listed[0]['preview']['result']['vpn'], 'moves')
        response = self.client.post(f"/api/v1/merge-requests/{created.json()['id']}/decision", headers=admin,
                                    json={'decision': 'approve'})
        self.assertEqual(response.json()['status'], 'approved', response.text)
        self.assertEqual(self.client.get('/api/v1/me', headers=bot).json()['id'], main)
        self.assertIsNotNone(other)


if __name__ == '__main__':
    unittest.main()
