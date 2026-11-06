"""Open registration: off by default, proof of work, limits, the "without access" list, cleanup and the API."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import unittest

from backend import registrations, sessions
from backend.authorization import AccessDenied, Principal, PrincipalKind, resolve_actor
from backend.credentials import PERMISSIONS
from tests import test_backend_http as http_fixture

NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)
PASSWORD = 'correct horse battery'


def solve(puzzle):
    nonce = 0
    while not registrations.solved(puzzle['challenge'], str(nonce), puzzle['bits']):
        nonce += 1
    return str(nonce)


class RegistrationTests(unittest.TestCase):
    setUp_http = http_fixture.BackendHTTPTests.setUp
    register_identity = http_fixture.BackendHTTPTests.register
    provision = http_fixture.BackendHTTPTests.provision

    def setUp(self):
        self.setUp_http()
        # Quicker puzzles for tests; the check is the same.
        self.bits = registrations.POW_BITS
        registrations.POW_BITS = 8
        self.addCleanup(setattr, registrations, 'POW_BITS', self.bits)

    def admin_actor(self):
        return resolve_actor(Principal('test', PrincipalKind.ACCOUNT, PERMISSIONS, account_id=self.admin.id),
                             self.identities)

    def code(self, call, *args, **kwargs):
        with self.assertRaises(AccessDenied) as caught:
            call(*args, **kwargs)
        return caught.exception.code

    def register(self, login, source='10.0.0.1', now=NOW, **kwargs):
        puzzle = registrations.challenge(self.db, source=source, now=now)
        return registrations.register(self.db, puzzle['challenge_id'], solve(puzzle), login=login,
                                      source=source, now=now, **kwargs)

    def test_closed_by_default_then_registers_without_access(self):
        self.assertEqual(self.code(registrations.challenge, self.db, source='s'), 'registration_closed')
        self.assertEqual(registrations.update(self.db, self.admin_actor(), open=True),
                         {'open': True, 'cleanup_days': None})
        result = self.register('Kim', password=PASSWORD)
        self.assertEqual((result['account']['login'], result['account']['role']), ('Kim', 'member'))
        self.credentials.authenticate('Bearer ' + result['session']['access_token'], now=NOW)
        sessions.sign_in(self.db, 'kim', PASSWORD, device='x', source='10.0.0.1', now=NOW)
        with self.db.connect() as conn:
            # No automatic request.
            self.assertIsNone(conn.execute('SELECT 1 FROM backend_access_requests WHERE account_id=?',
                                           (result['account']['id'],)).fetchone())

    def test_the_puzzle_is_needed_once_and_addresses_are_limited(self):
        registrations.update(self.db, self.admin_actor(), open=True)
        puzzle = registrations.challenge(self.db, source='s', now=NOW)
        wrong = next(str(n) for n in range(10_000) if not registrations.solved(puzzle['challenge'], str(n), 8))
        self.assertEqual(self.code(registrations.register, self.db, puzzle['challenge_id'], wrong, login='lee',
                                   source='s', now=NOW), 'registration_challenge_invalid')
        # The challenge is gone after any attempt.
        self.assertEqual(self.code(registrations.register, self.db, puzzle['challenge_id'], solve(puzzle),
                                   login='lee', source='s', now=NOW), 'registration_challenge_invalid')
        late = registrations.challenge(self.db, source='s', now=NOW)
        self.assertEqual(self.code(registrations.register, self.db, late['challenge_id'], solve(late), login='lee',
                                   source='s', now=NOW + registrations.CHALLENGE_TTL), 'registration_challenge_invalid')
        for index in range(registrations.PER_SOURCE):
            self.register(f'user{index}', source='t')
        self.assertEqual(self.code(self.register, 'one_more', source='t'), 'registration_rate_limited')
        self.register('one_more', source='t', now=NOW + registrations.PER_SOURCE_WINDOW)
        self.assertEqual(self.code(self.register, 'user0', source='u'), 'login_taken')

    def test_accounts_without_access_are_listed_apart_and_cleaned_up(self):
        registrations.update(self.db, self.admin_actor(), open=True)
        empty = self.register('empty_one')['account']['id']
        active = self.register('active_one')['account']['id']
        self.register_identity(202)
        member = self.provision(202).json()['account']['id']
        actor = self.admin_actor()
        from backend.accounts import AccountService
        listed = {item['id'] for item in AccountService(self.db).list(actor, limit=100, access='with')['items']}
        apart = {item['id'] for item in AccountService(self.db).list(actor, limit=100, access='without')['items']}
        self.assertTrue({member, self.admin.id} <= listed and not {empty, active} & listed)
        self.assertEqual(apart, {empty, active})
        cleanup = registrations.RegistrationCleanup(self.db)
        # Off unless the days are set.
        self.assertFalse(cleanup.run_one(now=NOW + timedelta(days=400)))
        registrations.update(self.db, actor, cleanup_days=30)
        # A sign-in keeps an account.
        with self.db.transaction() as conn:
            conn.execute('UPDATE backend_sessions SET used_at=? WHERE account_id=?',
                         ((NOW + timedelta(days=20)).isoformat(), active))
        self.assertTrue(cleanup.run_one(now=NOW + timedelta(days=31)))
        self.assertFalse(cleanup.run_one(now=NOW + timedelta(days=31)))
        with self.db.connect() as conn:
            deleted = {row['account_id'] for row in conn.execute(
                'SELECT account_id FROM backend_account_deletions WHERE completed_at IS NOT NULL').fetchall()}
        self.assertEqual(deleted, {empty})

    def test_api_service_access_and_settings(self):
        from backend.workstation_cli import SCOPES
        _, token = self.credentials.issue(PrincipalKind.ACCOUNT, SCOPES, account_id=self.admin.id)
        admin = {'Authorization': 'Bearer ' + token}
        self.assertEqual(self.client.get('/api/v1/registrations').json(), {'open': False})
        response = self.client.patch('/api/v1/system/registration', headers=admin,
                                     json={'open': True, 'cleanup_days': 60})
        self.assertEqual(response.json(), {'open': True, 'cleanup_days': 60}, response.text)
        response = self.client.patch('/api/v1/system/registration', headers=admin, json={'cleanup_days': None})
        self.assertEqual(response.json(), {'open': True, 'cleanup_days': None})
        puzzle = self.client.post('/api/v1/registrations/challenge').json()
        response = self.client.post('/api/v1/registrations', json={
            'challenge_id': puzzle['challenge_id'], 'nonce': solve(puzzle), 'login': 'mia', 'password': PASSWORD})
        self.assertEqual(response.status_code, 201, response.text)
        own = {'Authorization': 'Bearer ' + response.json()['session']['access_token']}
        access = self.client.get('/api/v1/services', headers=own).json()['items']
        self.assertEqual([(item['service'], item['access']) for item in access], [('vpn', 'requestable')])
        response = self.client.post('/api/v1/me/access-requests', headers={**own, 'Idempotency-Key': str(uuid4())})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(self.client.get('/api/v1/services', headers=own).json()['items'][0]['access'], 'pending')
        # With a request, the account is in the main list again.
        logins = [item.get('login') for item in self.client.get('/api/v1/accounts?limit=100', headers=admin)
                  .json()['items']]
        self.assertIn('mia', logins)


if __name__ == '__main__':
    unittest.main()
