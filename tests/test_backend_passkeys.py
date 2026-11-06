"""Passkeys: registration and sign-in with a software authenticator, replay and clone checks, and the API."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import struct
from unittest.mock import patch
from uuid import uuid4
import unittest

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import bytes_to_base64url

from backend import passkeys, sessions
from backend.authorization import AccessDenied
from tests import test_backend_http as http_fixture

NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)
RP = ('cab.example.com', 'https://cab.example.com')
# Tests patch it to stand for a configured relay.
RELYING_PARTY = passkeys.relying_party


class Authenticator:
    """A passkey on a device: one ES256 key, its counter, the account it was made for."""

    def __init__(self, rp_id=RP[0], origin=RP[1]):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.id = uuid4().bytes
        self.counter = 0
        self.rp_id, self.origin = rp_id, origin
        self.user = None

    def _client_data(self, kind, options):
        return json.dumps({'type': kind, 'challenge': options['challenge'], 'origin': self.origin,
                           'crossOrigin': False}).encode()

    def create(self, options):
        self.user = options['user']['id']
        numbers = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, 'big'), -3: numbers.y.to_bytes(32, 'big')})
        # User present, verified, attested credential data.
        data = (hashlib.sha256(self.rp_id.encode()).digest() + bytes([0x45]) + struct.pack('>I', self.counter)
                + bytes(16) + struct.pack('>H', len(self.id)) + self.id + cose)
        return {'id': bytes_to_base64url(self.id), 'rawId': bytes_to_base64url(self.id), 'type': 'public-key',
                'response': {'clientDataJSON': bytes_to_base64url(self._client_data('webauthn.create', options)),
                             'attestationObject': bytes_to_base64url(cbor2.dumps(
                                 {'fmt': 'none', 'attStmt': {}, 'authData': data})),
                             'transports': ['internal']},
                'clientExtensionResults': {}, 'authenticatorAttachment': 'platform'}

    def get(self, options, *, counter=None, user=None):
        self.counter = self.counter + 1 if counter is None else counter
        data = hashlib.sha256(self.rp_id.encode()).digest() + bytes([0x05]) + struct.pack('>I', self.counter)
        client = self._client_data('webauthn.get', options)
        signature = self.key.sign(data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {'id': bytes_to_base64url(self.id), 'rawId': bytes_to_base64url(self.id), 'type': 'public-key',
                'response': {'clientDataJSON': bytes_to_base64url(client),
                             'authenticatorData': bytes_to_base64url(data),
                             'signature': bytes_to_base64url(signature), 'userHandle': user or self.user},
                'clientExtensionResults': {}}


class PasskeyTests(unittest.TestCase):
    setUp_http = http_fixture.BackendHTTPTests.setUp
    register_identity = http_fixture.BackendHTTPTests.register
    provision = http_fixture.BackendHTTPTests.provision

    def setUp(self):
        self.setUp_http()
        relay = patch.object(passkeys, 'relying_party', return_value=RP)
        relay.start()
        self.addCleanup(relay.stop)

    def account(self):
        account_id = str(uuid4())
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_accounts(id, status) VALUES (?, 'approved')", (account_id,))
        return account_id

    def code(self, call, *args, **kwargs):
        with self.assertRaises(AccessDenied) as caught:
            call(*args, **kwargs)
        return caught.exception.code

    def enroll(self, account_id, device=None):
        device = device or Authenticator()
        started = passkeys.registration_options(self.db, account_id, now=NOW)
        passkeys.register(self.db, account_id, started['challenge_id'], device.create(started['options']),
                          'Phone', now=NOW)
        return device

    def sign_in(self, device, now=NOW, **kwargs):
        started = passkeys.sign_in_options(self.db, source='10.0.0.1', now=now)
        return passkeys.sign_in(self.db, started['challenge_id'], device.get(started['options'], **kwargs),
                                device='Phone', source='10.0.0.1', now=now)

    def test_relying_party_is_the_cabinets_public_origin(self):
        relying_party = RELYING_PARTY
        for address, host in (('https://cab.example', 'cab.example'), ('https://cab.example:8443', 'cab.example')):
            with patch('backend.web_cabinet.origin', return_value=address):
                self.assertEqual(relying_party(None), (host, address))
        with patch('backend.web_cabinet.origin', return_value=None):
            self.assertEqual(self.code(relying_party, None), 'passkeys_unavailable')

    def test_register_and_sign_in_without_a_login(self):
        account_id = self.account()
        started = passkeys.registration_options(self.db, account_id, now=NOW)
        options = started['options']
        self.assertEqual((options['rp']['id'], options['authenticatorSelection']['residentKey'],
                          options['authenticatorSelection']['userVerification']),
                         ('cab.example.com', 'required', 'required'))
        device = Authenticator()
        credential = device.create(options)
        listed = passkeys.register(self.db, account_id, started['challenge_id'], credential, ' Phone ', now=NOW)
        self.assertEqual(listed['name'], 'Phone')
        # A challenge is used once.
        self.assertEqual(self.code(passkeys.register, self.db, account_id, started['challenge_id'], credential,
                                   now=NOW), 'passkey_challenge_invalid')
        # The same passkey is not offered again.
        again = passkeys.registration_options(self.db, account_id, now=NOW)['options']
        self.assertEqual([c['id'] for c in again['excludeCredentials']], [credential['id']])
        tokens = self.sign_in(device)
        principal = self.credentials.authenticate('Bearer ' + tokens['access_token'], now=NOW)
        self.assertEqual(principal.account_id, account_id)
        with self.db.connect() as conn:
            self.assertEqual([s['method'] for s in sessions.listed(conn, account_id, now=NOW)], ['passkey'])

    def test_wrong_origin_challenge_clone_and_handle_are_refused(self):
        account_id = self.account()
        started = passkeys.registration_options(self.db, account_id, now=NOW)
        phishing = Authenticator(origin='https://cab.example.com.evil')
        self.assertEqual(self.code(passkeys.register, self.db, account_id, started['challenge_id'],
                                   phishing.create(started['options']), now=NOW), 'passkey_invalid')
        other = self.account()
        started = passkeys.registration_options(self.db, account_id, now=NOW)
        # A challenge belongs to the account that asked for it.
        self.assertEqual(self.code(passkeys.register, self.db, other, started['challenge_id'],
                                   Authenticator().create(started['options']), now=NOW), 'passkey_challenge_invalid')
        device = self.enroll(account_id)
        self.sign_in(device)
        # A counter that does not grow means a copied authenticator.
        self.assertEqual(self.code(self.sign_in, device, counter=1), 'sign_in_failed')
        self.assertEqual(self.code(self.sign_in, device, user=bytes_to_base64url(other.encode())), 'sign_in_failed')
        self.assertEqual(self.code(self.sign_in, Authenticator()), 'sign_in_failed')
        late = passkeys.sign_in_options(self.db, source='10.0.0.1', now=NOW)
        self.assertEqual(self.code(passkeys.sign_in, self.db, late['challenge_id'], device.get(late['options']),
                                   device='x', source='10.0.0.1', now=NOW + passkeys.CHALLENGE_TTL),
                         'passkey_challenge_invalid')
        for _ in range(sessions.SOURCE_FAILURES):
            self.code(self.sign_in, Authenticator())
        self.assertEqual(self.code(passkeys.sign_in_options, self.db, source='10.0.0.1', now=NOW),
                         'sign_in_rate_limited')

    def test_blocked_or_deleted_accounts_do_not_sign_in(self):
        account_id = self.account()
        device = self.enroll(account_id)
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_accounts SET status='disabled' WHERE id=?", (account_id,))
        self.assertEqual(self.code(self.sign_in, device), 'sign_in_failed')
        from backend import account_deletions
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_accounts SET status='approved' WHERE id=?", (account_id,))
            account_deletions.begin(conn, account_id)
            self.assertEqual(passkeys.count(conn, account_id), 0)

    def test_api_adds_lists_removes_and_an_administrator_removes_all(self):
        from backend.authorization import PrincipalKind
        from backend.workstation_cli import SCOPES
        self.register_identity(202)
        member = self.provision(202).json()['account']['id']
        adapter = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '202'}
        # Telegram's word does not add a sign-in method.
        response = self.client.post('/api/v1/me/passkeys/options', headers=adapter)
        self.assertEqual(response.json()['error']['code'], 'passkey_not_through_adapter')
        _, token = self.credentials.issue(PrincipalKind.ACCOUNT, SCOPES, account_id=member)
        own = {'Authorization': 'Bearer ' + token}
        started = self.client.post('/api/v1/me/passkeys/options', headers=own).json()
        device = Authenticator()
        response = self.client.post('/api/v1/me/passkeys', headers=own, json={
            'challenge_id': started['challenge_id'], 'credential': device.create(started['options']), 'name': 'Mac'})
        self.assertEqual(response.status_code, 201, response.text)
        passkey = response.json()['id']
        self.assertEqual(self.client.get('/api/v1/me', headers=adapter).json()['passkeys'], 1)
        started = self.client.post('/api/v1/sessions/passkey/options').json()
        response = self.client.post('/api/v1/sessions/passkey', json={
            'challenge_id': started['challenge_id'], 'credential': device.get(started['options'])})
        self.assertEqual(response.status_code, 201, response.text)
        _, admin_token = self.credentials.issue(PrincipalKind.ACCOUNT, SCOPES, account_id=self.admin.id)
        admin = {'Authorization': 'Bearer ' + admin_token}
        self.assertEqual(self.client.get(f'/api/v1/accounts/{member}', headers=admin).json()['passkeys'], 1)
        # Removing passkeys in the bot is fine: nothing secret travels.
        listed = self.client.get('/api/v1/me/passkeys', headers=adapter).json()['items']
        self.assertEqual([item['name'] for item in listed], ['Mac'])
        self.assertEqual(self.client.delete(f'/api/v1/me/passkeys/{passkey}', headers=adapter).status_code, 204)
        self.enroll(member)
        response = self.client.delete(f'/api/v1/accounts/{member}/passkeys', headers=admin)
        self.assertEqual(response.json()['passkeys'], 0, response.text)


if __name__ == '__main__':
    unittest.main()
