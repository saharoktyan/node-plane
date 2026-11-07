"""A cabinet session confirms itself again before a dangerous action; issued
tokens, Workstation and the bot are not asked."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import unittest

from backend import confirmations, logins, sessions
from backend.authorization import AccessDenied, PrincipalKind
from tests import test_backend_http as http_fixture

PASSWORD = 'correct horse battery'
DANGEROUS = f'/api/v1/system/backups/catalog/{uuid4()}/delete'


class ConfirmationTests(unittest.TestCase):
    setUp = http_fixture.BackendHTTPTests.setUp

    def session(self):
        now = datetime.now(timezone.utc)
        with self.db.transaction() as conn:
            logins.set_own(conn, self.admin.id, 'boss', now)
            sessions.set_password(conn, self.admin.id, PASSWORD, now=now)
        tokens = self.client.post('/api/v1/sessions', json={'login': 'boss', 'password': PASSWORD}).json()
        return {'Authorization': 'Bearer ' + tokens['access_token']}

    def error(self, response):
        return response.json().get('error', {}).get('code')

    def test_a_session_confirms_with_its_password_before_a_dangerous_action(self):
        own = self.session()
        status = self.client.get('/api/v1/me/confirmation', headers=own).json()
        self.assertEqual((status['required'], status['confirmed_until'], status['password'], status['passkeys']),
                         (True, None, True, False))
        response = self.client.post(DANGEROUS, headers=own, json={'checksum': 'a' * 64})
        self.assertEqual((response.status_code, self.error(response)), (403, 'confirmation_required'))
        response = self.client.post('/api/v1/me/confirmation', headers=own, json={'password': 'wrong password'})
        self.assertEqual((response.status_code, self.error(response)), (403, 'password_incorrect'))
        response = self.client.post('/api/v1/me/confirmation', headers=own, json={'password': PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNotNone(response.json()['confirmed_until'])
        response = self.client.post(DANGEROUS, headers=own, json={'checksum': 'a' * 64})
        self.assertNotEqual(self.error(response), 'confirmation_required')
        # A safe action never asks.
        self.assertEqual(self.client.get('/api/v1/system/backups/catalog', headers=own).status_code, 200)

    def test_tokens_without_a_session_are_not_asked(self):
        from backend.workstation_cli import SCOPES
        _, token = self.credentials.issue(PrincipalKind.ACCOUNT, SCOPES, account_id=self.admin.id)
        headers = {'Authorization': 'Bearer ' + token}
        self.assertFalse(self.client.get('/api/v1/me/confirmation', headers=headers).json()['required'])
        response = self.client.post(DANGEROUS, headers=headers, json={'checksum': 'a' * 64})
        self.assertNotEqual(self.error(response), 'confirmation_required')
        response = self.client.post('/api/v1/me/confirmation', headers=headers, json={'password': PASSWORD})
        self.assertEqual(self.error(response), 'confirmation_not_needed')

    def test_a_confirmation_runs_out_and_wrong_passwords_are_limited(self):
        own = self.session()
        self.client.post('/api/v1/me/confirmation', headers=own, json={'password': PASSWORD})
        with self.db.connect() as conn:
            row = conn.execute('SELECT confirmed_at FROM backend_session_confirmations').fetchone()
        later = datetime.fromisoformat(row['confirmed_at']) + confirmations.TTL + timedelta(seconds=1)
        actor = self.actor(own)
        with self.assertRaises(AccessDenied) as caught:
            confirmations.require(self.db, actor, now=later)
        self.assertEqual(caught.exception.code, 'confirmation_required')
        for _ in range(sessions.LOGIN_FAILURES):
            self.client.post('/api/v1/me/confirmation', headers=own, json={'password': 'wrong password'})
        response = self.client.post('/api/v1/me/confirmation', headers=own, json={'password': PASSWORD})
        self.assertEqual((response.status_code, self.error(response)), (429, 'sign_in_rate_limited'))

    def actor(self, headers):
        from backend.authorization import resolve_actor
        principal = self.credentials.authenticate(headers['Authorization'])
        return resolve_actor(principal, self.identities)


if __name__ == '__main__':
    unittest.main()
