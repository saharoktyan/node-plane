"""Account logins: rules, the 30-day change limit and hold, and the API."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import unittest

from backend import logins
from backend.authorization import AccessDenied
from tests import test_backend_http as http_fixture

NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)


class LoginTests(unittest.TestCase):
    setUp = http_fixture.BackendHTTPTests.setUp
    register = http_fixture.BackendHTTPTests.register
    provision = http_fixture.BackendHTTPTests.provision

    def account(self):
        account_id = str(uuid4())
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_accounts(id, status) VALUES (?, 'approved')", (account_id,))
        return account_id

    def code(self, call, *args, **kwargs):
        with self.assertRaises(AccessDenied) as caught:
            with self.db.transaction() as conn:
                call(conn, *args, **kwargs)
        return caught.exception.code

    def test_rules(self):
        for bad in ('ab', 'a' * 33, '_lead', 'with space', 'кириллица', 'a@b'):
            self.assertEqual(self.code(lambda conn, value: logins.checked(value), bad), 'login_invalid', bad)
        self.assertEqual(self.code(lambda conn: logins.checked('Admin')), 'login_reserved')
        self.assertEqual(logins.checked('Ops.Team_1-x'), ('Ops.Team_1-x', 'ops.team_1-x'))

    def test_unique_without_case_change_limit_and_hold(self):
        alice, bob = self.account(), self.account()
        with self.db.transaction() as conn:
            self.assertEqual(logins.set_own(conn, alice, 'Alice', NOW), 'Alice')
        self.assertEqual(self.code(logins.set_own, bob, 'ALICE', NOW), 'login_taken')
        with self.db.transaction() as conn:
            # The first change may follow the first login at once.
            logins.set_own(conn, alice, 'alice2', NOW)
            self.assertEqual(logins.change_available_at(conn, alice, NOW), (NOW + timedelta(days=30)).isoformat())
            # The case of the current login may change at any time.
            self.assertEqual(logins.set_own(conn, alice, 'ALICE2', NOW), 'ALICE2')
        self.assertEqual(self.code(logins.set_own, alice, 'alice3', NOW + timedelta(days=29)), 'login_change_too_soon')
        # The old login stays held for its account for 30 days.
        self.assertEqual(self.code(logins.set_own, bob, 'alice', NOW + timedelta(days=29)), 'login_taken')
        with self.db.transaction() as conn:
            self.assertEqual(logins.set_own(conn, bob, 'alice', NOW + timedelta(days=30)), 'alice')
            # An administrator changes a login at any time; Alice may take hers back after the limit.
            logins.claim(conn, alice, 'alice-admin', 'admin', NOW + timedelta(days=30))
            self.assertEqual(logins.current(conn, alice), 'alice-admin')
            logins.set_own(conn, alice, 'ALICE2', NOW + timedelta(days=31))
            self.assertEqual(logins.current(conn, alice), 'ALICE2')
            logins.release(conn, alice, NOW + timedelta(days=31))
            self.assertIsNone(logins.current(conn, alice))
            self.assertFalse(logins.available(conn, 'alice2', bob, NOW + timedelta(days=60)))
            self.assertTrue(logins.available(conn, 'alice2', bob, NOW + timedelta(days=61)))

    def test_api_sets_own_login_suggests_telegram_username_and_admin_changes(self):
        from backend.authorization import PrincipalKind
        from backend.workstation_cli import SCOPES
        self.register(202, username='carol_tg')
        member = self.provision(202).json()['account']['id']
        adapter = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '202'}
        me = self.client.get('/api/v1/me', headers=adapter).json()
        self.assertEqual((me['login'], me['login_suggestion']), (None, 'carol_tg'))
        response = self.client.put('/api/v1/me/login', headers=adapter, json={'login': 'root'})
        self.assertEqual(response.json()['error']['code'], 'login_reserved')
        me = self.client.put('/api/v1/me/login', headers=adapter, json={'login': 'Carol'}).json()
        self.assertEqual((me['login'], me['login_suggestion'], me['login_change_available_at']), ('Carol', None, None))
        me = self.client.put('/api/v1/me/login', headers=adapter, json={'login': 'carol2'}).json()
        self.assertIsNotNone(me['login_change_available_at'])
        response = self.client.put('/api/v1/me/login', headers=adapter, json={'login': 'carol3'})
        self.assertEqual((response.status_code, response.json()['error']['code']), (409, 'login_change_too_soon'))
        _, token = self.credentials.issue(PrincipalKind.ACCOUNT, SCOPES, account_id=self.admin.id)
        admin = {'Authorization': 'Bearer ' + token}
        account = self.client.get(f'/api/v1/accounts/{member}', headers=admin).json()
        self.assertEqual(account['login'], 'carol2')
        response = self.client.patch(f'/api/v1/accounts/{member}', json={'login': 'carol-x'},
            headers={**admin, 'Idempotency-Key': str(uuid4()), 'If-Match': f'"{account["revision"]}"'})
        self.assertEqual(response.json()['login'], 'carol-x', response.text)
        listed = self.client.get('/api/v1/accounts', headers=admin).json()['items']
        self.assertIn('carol-x', [item.get('login') for item in listed])
