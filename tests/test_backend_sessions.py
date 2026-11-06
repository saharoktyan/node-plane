"""Passwords and sessions: sign-in, its limits, refresh rotation and replay, ending sessions, and the API."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import unittest

from backend import logins, sessions
from backend.authorization import AccessDenied
from tests import test_backend_http as http_fixture

NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)
PASSWORD = 'correct horse battery'


class SessionTests(unittest.TestCase):
    setUp = http_fixture.BackendHTTPTests.setUp
    register = http_fixture.BackendHTTPTests.register
    provision = http_fixture.BackendHTTPTests.provision

    def account(self, login='alice', password=PASSWORD):
        account_id = str(uuid4())
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_accounts(id, status) VALUES (?, 'approved')", (account_id,))
            logins.set_own(conn, account_id, login, NOW)
            if password:
                sessions.set_password(conn, account_id, password, now=NOW)
        return account_id

    def code(self, call, *args, **kwargs):
        with self.assertRaises(AccessDenied) as caught:
            call(*args, **kwargs)
        return caught.exception.code

    def sign_in(self, login='alice', password=PASSWORD, source='10.0.0.1', now=NOW):
        return sessions.sign_in(self.db, login, password, device='Firefox', source=source, now=now)

    def test_password_rules(self):
        for bad in ('short', 'x' * 257, 'Alice12345'):
            self.assertEqual(self.code(sessions.checked_password, bad, 'alice12345'), 'password_invalid', bad)
        self.assertEqual(sessions.checked_password('ten chars!'), 'ten chars!')

    def test_sign_in_does_not_say_what_was_wrong_and_is_limited(self):
        self.account()
        self.account('bob', None)
        for login, password in (('alice', 'wrong password'), ('nobody', PASSWORD), ('bob', PASSWORD)):
            self.assertEqual(self.code(self.sign_in, login, password), 'sign_in_failed')
        # The login is case-insensitive.
        tokens = self.sign_in('ALICE')
        self.assertTrue(tokens['access_token'].startswith('np_') and tokens['refresh_token'].startswith('nps_'))
        for _ in range(sessions.LOGIN_FAILURES):
            self.assertEqual(self.code(self.sign_in, 'alice', 'wrong password'), 'sign_in_failed')
        # Even the right password waits once the login has too many failures.
        self.assertEqual(self.code(self.sign_in), 'sign_in_rate_limited')
        self.assertEqual(self.code(self.sign_in, source='10.0.0.2'), 'sign_in_rate_limited')
        self.sign_in(now=NOW + sessions.WINDOW + timedelta(seconds=1))
        # A source that fails with many logins waits as well.
        for index in range(sessions.SOURCE_FAILURES):
            self.code(self.sign_in, f'user{index}', PASSWORD, source='10.0.0.9')
        self.assertEqual(self.code(self.sign_in, source='10.0.0.9'), 'sign_in_rate_limited')

    def test_refresh_rotates_and_a_replay_ends_the_session(self):
        account_id = self.account()
        first = self.sign_in()
        second = sessions.refresh(self.db, first['refresh_token'], now=NOW + timedelta(minutes=20))
        self.assertEqual(second['session_id'], first['session_id'])
        self.assertNotEqual(second['refresh_token'], first['refresh_token'])
        principal = self.credentials.authenticate('Bearer ' + second['access_token'], now=NOW + timedelta(minutes=21))
        self.assertEqual(principal.account_id, account_id)
        # The first token was used again: only a copy could be.
        self.assertEqual(self.code(sessions.refresh, self.db, first['refresh_token'],
                                   now=NOW + timedelta(minutes=22)), 'session_revoked')
        self.assertEqual(self.code(sessions.refresh, self.db, second['refresh_token'],
                                   now=NOW + timedelta(minutes=23)), 'invalid_credentials')
        self.assertEqual(self.code(self.credentials.authenticate, 'Bearer ' + second['access_token'],
                                   now=NOW + timedelta(minutes=23)), 'invalid_credentials')
        # A session without refreshes ends after the idle time.
        third = self.sign_in()
        self.assertEqual(self.code(sessions.refresh, self.db, third['refresh_token'],
                                   now=NOW + sessions.IDLE_TTL), 'invalid_credentials')

    def test_a_password_change_needs_the_current_one_and_ends_other_sessions(self):
        account_id = self.account()
        kept, other = self.sign_in(), self.sign_in()
        with self.db.transaction() as conn:
            self.assertEqual(self.code(sessions.set_password, conn, account_id, 'new password!'), 'password_incorrect')
            self.assertEqual(self.code(sessions.set_password, conn, account_id, 'new password!', current='nope'),
                             'password_incorrect')
            sessions.set_password(conn, account_id, 'new password!', current=PASSWORD, keep_session=kept['session_id'])
            self.assertEqual([s['id'] for s in sessions.listed(conn, account_id, now=NOW)], [kept['session_id']])
        self.assertEqual(self.code(sessions.refresh, self.db, other['refresh_token'], now=NOW), 'invalid_credentials')
        self.assertEqual(self.code(self.sign_in), 'sign_in_failed')
        self.sign_in(password='new password!')

    def test_api_signs_in_lists_and_ends_sessions(self):
        from backend.authorization import PrincipalKind
        from backend.workstation_cli import SCOPES
        self.register(202)
        member = self.provision(202).json()['account']['id']
        adapter = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '202'}
        self.client.put('/api/v1/me/login', headers=adapter, json={'login': 'carol'})
        # Passwords do not travel through Telegram.
        response = self.client.put('/api/v1/me/password', headers=adapter, json={'password': PASSWORD})
        self.assertEqual((response.status_code, response.json()['error']['code']),
                         (403, 'password_not_through_adapter'))
        _, token = self.credentials.issue(PrincipalKind.ACCOUNT, SCOPES, account_id=member)
        response = self.client.put('/api/v1/me/password', headers={'Authorization': 'Bearer ' + token},
                                   json={'password': PASSWORD})
        self.assertTrue(response.json()['has_password'], response.text)
        response = self.client.post('/api/v1/sessions', json={'login': 'carol', 'password': 'wrong one!'})
        self.assertEqual((response.status_code, response.json()['error']['code']), (401, 'sign_in_failed'))
        first = self.client.post('/api/v1/sessions', json={'login': 'carol', 'password': PASSWORD},
                                 headers={'User-Agent': 'Firefox'}).json()
        second = self.client.post('/api/v1/sessions', json={'login': 'carol', 'password': PASSWORD,
                                                             'device': 'Phone'}).json()
        own = {'Authorization': 'Bearer ' + first['access_token']}
        listed = self.client.get('/api/v1/me/sessions', headers=own).json()['items']
        self.assertEqual({(s['device'], s['current']) for s in listed}, {('Firefox', True), ('Phone', False)})
        refreshed = self.client.post('/api/v1/sessions/refresh', json={'refresh_token': second['refresh_token']})
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        _, admin_token = self.credentials.issue(PrincipalKind.ACCOUNT, SCOPES, account_id=self.admin.id)
        admin = {'Authorization': 'Bearer ' + admin_token}
        account = self.client.get(f'/api/v1/accounts/{member}', headers=admin).json()
        self.assertEqual((account['has_password'], account['sessions']), (True, 2))
        # Sign out everywhere else, then here.
        self.assertEqual(self.client.delete('/api/v1/me/sessions', headers=own).status_code, 204)
        self.assertEqual([s['current'] for s in self.client.get('/api/v1/me/sessions', headers=own).json()['items']],
                         [True])
        self.assertEqual(self.client.delete('/api/v1/me/sessions/current', headers=own).status_code, 204)
        self.assertEqual(self.client.get('/api/v1/me/sessions', headers=own).status_code, 401)
        third = self.client.post('/api/v1/sessions', json={'login': 'carol', 'password': PASSWORD}).json()
        response = self.client.delete(f'/api/v1/accounts/{member}/sessions', headers=admin)
        self.assertEqual(response.json()['sessions'], 0, response.text)
        response = self.client.post('/api/v1/sessions/refresh', json={'refresh_token': third['refresh_token']})
        self.assertEqual(response.status_code, 401)

    def test_account_deletion_ends_sessions_and_drops_the_password(self):
        account_id = self.account()
        tokens = self.sign_in()
        from backend import account_deletions
        with self.db.transaction() as conn:
            account_deletions.begin(conn, account_id)
            self.assertFalse(sessions.has_password(conn, account_id))
        self.assertEqual(self.code(sessions.refresh, self.db, tokens['refresh_token'], now=NOW), 'invalid_credentials')


if __name__ == '__main__':
    unittest.main()
