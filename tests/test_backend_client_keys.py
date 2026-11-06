"""Signed requests: Workstation through the relay acts as its staff account,
the remote bot as the Telegram adapter; the relay can neither forge nor
replay them."""
from datetime import datetime, timedelta, timezone
import json
import secrets
import unittest
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from backend import client_keys
from tests import test_backend_http


class Client:
    """A client with its own key, signing like Workstation and the bot do."""

    def __init__(self):
        self.key = Ed25519PrivateKey.generate()
        self.public = client_keys.encode(self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
        self.id = None

    def sign(self, method, target, body=b'', *, at=None, nonce=None, delegated=None):
        stamp = int((at or datetime.now(timezone.utc)).timestamp())
        nonce = nonce or secrets.token_urlsafe(16)
        signature = self.key.sign(client_keys.message(method, target, body, stamp, nonce, delegated))
        return f'NP-Signature key={self.id}, ts={stamp}, nonce={nonce}, sig={client_keys.encode(signature)}'


class ClientKeyTests(unittest.TestCase):
    setUp = test_backend_http.BackendHTTPTests.setUp

    def client_key(self, kind='workstation', account_id=None):
        client = Client()
        with self.db.transaction() as conn:
            client.id = client_keys.register(conn, kind, client.public, name='Laptop',
                                             account_id=account_id if kind == 'bot' else account_id or self.admin.id)
        return client

    def call(self, client, method, path, body=None, *, relay=True, extra=None, **sign):
        data = b'' if body is None else json.dumps(body).encode()
        delegated = (extra or {}).get('X-Node-Plane-Telegram-User-ID')
        headers = {'Authorization': client.sign(method, path, data, delegated=delegated, **sign), **(extra or {})}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        if relay:
            headers['X-Node-Plane-Via'] = 'relay'
        return self.client.request(method, path, content=data, headers=headers)

    def test_a_workstation_key_acts_as_its_staff_account(self):
        client = self.client_key()
        response = self.call(client, 'GET', '/api/v1/me')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['id'], self.admin.id)
        self.assertEqual(self.call(client, 'GET', '/api/v1/nodes?limit=5').status_code, 200)
        # A body is signed too.
        response = self.call(client, 'PUT', '/api/v1/me/login', {'login': 'ops'},
                             extra={'Idempotency-Key': str(uuid4())})
        self.assertEqual(response.status_code, 200, response.text)
        # System routes stay closed through the relay.
        response = self.call(client, 'GET', '/api/v1/system/updates')
        self.assertEqual(response.json()['error']['code'], 'not_available_through_relay')
        with self.db.connect() as conn:
            self.assertIsNotNone(conn.execute('SELECT last_used_at FROM backend_client_keys WHERE id=?',
                                              (client.id,)).fetchone()['last_used_at'])

    def test_forged_late_replayed_and_revoked_requests_are_refused(self):
        client = self.client_key()
        # Another key, a changed path, a changed body.
        stranger = Client()
        stranger.id = client.id
        self.assertEqual(self.call(stranger, 'GET', '/api/v1/me').status_code, 401)
        headers = {'Authorization': client.sign('GET', '/api/v1/me'), 'X-Node-Plane-Via': 'relay'}
        self.assertEqual(self.client.get('/api/v1/nodes', headers=headers).status_code, 401)
        headers = {'Authorization': client.sign('PUT', '/api/v1/me/login', b'{"login":"ops"}'),
                   'Content-Type': 'application/json', 'Idempotency-Key': str(uuid4())}
        self.assertEqual(self.client.put('/api/v1/me/login', content=b'{"login":"root2"}',
                                         headers=headers).status_code, 401)
        # Two minutes off.
        late = self.call(client, 'GET', '/api/v1/me', at=datetime.now(timezone.utc) - timedelta(minutes=2))
        self.assertEqual(late.json()['error']['code'], 'request_time_invalid')
        # The same nonce twice.
        self.assertEqual(self.call(client, 'GET', '/api/v1/me', nonce='n' * 22).status_code, 200)
        replay = self.call(client, 'GET', '/api/v1/me', nonce='n' * 22)
        self.assertEqual(replay.json()['error']['code'], 'request_replayed')
        # Unknown key, malformed header, revoked key.
        with self.db.transaction() as conn:
            client_keys.revoke(conn, client.id)
        self.assertEqual(self.call(client, 'GET', '/api/v1/me').status_code, 401)
        self.assertEqual(self.client.get('/api/v1/me', headers={'Authorization': 'NP-Signature key=x'}).status_code, 401)

    def test_a_key_stops_working_when_the_account_is_no_longer_staff(self):
        member = test_backend_http.bootstrap_admin(self.identities, 202)
        client = self.client_key(account_id=member.id)
        self.assertEqual(self.call(client, 'GET', '/api/v1/me').status_code, 200)
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_accounts SET role='member' WHERE id=?", (member.id,))
        self.assertEqual(self.call(client, 'GET', '/api/v1/me').status_code, 401)

    def test_the_bot_key_acts_for_telegram_users_through_the_relay(self):
        bot = self.client_key('bot')
        delegated = {'X-Node-Plane-Telegram-User-ID': '101'}
        response = self.call(bot, 'GET', '/api/v1/me', extra=delegated)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['id'], self.admin.id)
        # The relay cannot swap the user the bot acts for.
        headers = {'Authorization': bot.sign('GET', '/api/v1/me', delegated='101'), 'X-Node-Plane-Via': 'relay',
                   'X-Node-Plane-Telegram-User-ID': '202'}
        self.assertEqual(self.client.get('/api/v1/me', headers=headers).status_code, 401)
        # A Workstation key cannot act for someone else, and the adapter token still cannot come through.
        workstation = self.client_key()
        response = self.call(workstation, 'GET', '/api/v1/me', extra=delegated)
        self.assertEqual(response.json()['error']['code'], 'not_available_through_relay')
        response = self.client.get('/api/v1/me', headers={**self.headers, **delegated, 'X-Node-Plane-Via': 'relay'})
        self.assertEqual(response.json()['error']['code'], 'not_available_through_relay')

    def test_keys_are_checked_when_stored(self):
        with self.db.transaction() as conn:
            for value in ('', 'x' * 42, 'short', None):
                with self.assertRaises(client_keys.AccessDenied):
                    client_keys.register(conn, 'workstation', value, name='x', account_id=self.admin.id)
            key = Client().public
            client_keys.register(conn, 'workstation', key, name='x', account_id=self.admin.id)
            with self.assertRaises(client_keys.AccessDenied):
                client_keys.register(conn, 'bot', key, name='y')
            with self.assertRaises(ValueError):
                client_keys.register(conn, 'bot', Client().public, name='y', account_id=self.admin.id)


class PairingTests(unittest.TestCase):
    setUp = test_backend_http.BackendHTTPTests.setUp

    def account_headers(self, account_id=None):
        from backend.authorization import PrincipalKind
        from backend.workstation_cli import SCOPES
        _, token = self.credentials.issue(PrincipalKind.ACCOUNT, SCOPES, account_id=account_id or self.admin.id)
        return {'Authorization': 'Bearer ' + token}

    def ask(self, client, name='Laptop', via='relay'):
        headers = {'X-Node-Plane-Via': via, 'X-Node-Plane-Client': '198.51.100.7'} if via else {}
        return self.client.post('/api/v1/workstation-pairings', json={'public_key': client.public, 'name': name},
                                headers=headers)

    def test_workstation_links_with_a_code_a_staff_member_confirms(self):
        workstation = Client()
        answer = self.ask(workstation)
        self.assertEqual(answer.status_code, 201, answer.text)
        pairing = answer.json()
        self.assertRegex(pairing['code'], r'^[A-Z2-9]{4}-[A-Z2-9]{4}$')
        self.assertIsNone(pairing['url'])
        state = self.client.get('/api/v1/workstation-pairings/' + pairing['id']).json()
        self.assertEqual((state['status'], state['key_id']), ('pending', None))
        headers = self.account_headers()
        code = pairing['code'].lower().replace('-', ' ')
        preview = self.client.get('/api/v1/workstation-pairings/code/' + code, headers=headers).json()
        self.assertEqual((preview['name'], preview['source']), ('Laptop', '198.51.100.7'))
        decision = self.client.post(f'/api/v1/workstation-pairings/code/{code}/approve', headers=headers)
        self.assertEqual(decision.json(), {'status': 'approved', 'name': 'Laptop'})
        state = self.client.get('/api/v1/workstation-pairings/' + pairing['id']).json()
        self.assertEqual(state['status'], 'approved')
        # The linked key now signs as the account, and the code is used up.
        workstation.id = state['key_id']
        headers_signed = {'Authorization': workstation.sign('GET', '/api/v1/me'), 'X-Node-Plane-Via': 'relay'}
        self.assertEqual(self.client.get('/api/v1/me', headers=headers_signed).json()['id'], self.admin.id)
        self.assertEqual(self.client.post(f'/api/v1/workstation-pairings/code/{code}/approve',
                                          headers=headers).json()['error']['code'], 'pairing_not_found')
        listed = self.client.get('/api/v1/me/workstations', headers=headers).json()['items']
        self.assertEqual([item['name'] for item in listed], ['Laptop'])
        self.assertEqual(self.client.get(f'/api/v1/accounts/{self.admin.id}', headers=headers).json()['workstations'], 1)
        # Unlinking stops the key.
        self.assertEqual(self.client.delete('/api/v1/me/workstations/' + workstation.id, headers=headers).status_code, 204)
        headers_signed = {'Authorization': workstation.sign('GET', '/api/v1/me'), 'X-Node-Plane-Via': 'relay'}
        self.assertEqual(self.client.get('/api/v1/me', headers=headers_signed).status_code, 401)

    def test_only_a_signed_in_staff_member_confirms(self):
        pairing = self.ask(Client()).json()
        code = pairing['code']
        # Not through Telegram.
        response = self.client.post(f'/api/v1/workstation-pairings/code/{code}/approve',
                                    headers={**self.headers, 'X-Node-Plane-Telegram-User-ID': '101'})
        self.assertEqual(response.json()['error']['code'], 'pairing_not_through_adapter')
        member = test_backend_http.bootstrap_admin(self.identities, 303)
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_accounts SET role='member' WHERE id=?", (member.id,))
        response = self.client.get('/api/v1/workstation-pairings/code/' + code, headers=self.account_headers(member.id))
        self.assertEqual(response.json()['error']['code'], 'permission_denied')
        rejected = self.client.post(f'/api/v1/workstation-pairings/code/{code}/reject', headers=self.account_headers())
        self.assertEqual(rejected.json()['status'], 'rejected')
        self.assertEqual(self.client.get('/api/v1/workstation-pairings/' + pairing['id']).json()['status'], 'rejected')

    def test_requests_expire_and_are_limited_per_source(self):
        from backend import workstation_pairings
        late = workstation_pairings.request(self.db, Client().public, 'Old', source='a',
                                            now=datetime.now(timezone.utc) - timedelta(minutes=11))
        self.assertEqual(self.client.get('/api/v1/workstation-pairings/' + late['id']).json()['status'], 'expired')
        response = self.client.post(f"/api/v1/workstation-pairings/code/{late['code']}/approve",
                                    headers=self.account_headers())
        self.assertEqual(response.json()['error']['code'], 'pairing_not_found')
        for _ in range(workstation_pairings.PER_SOURCE):
            self.assertEqual(self.ask(Client()).status_code, 201)
        self.assertEqual(self.ask(Client()).json()['error']['code'], 'pairing_rate_limited')
        self.assertEqual(self.ask(Client(), via=None).status_code, 201, 'another source')

    def test_an_administrator_unlinks_every_workstation_of_an_account(self):
        client = Client()
        with self.db.transaction() as conn:
            client.id = client_keys.register(conn, 'workstation', client.public, name='x', account_id=self.admin.id)
        response = self.client.delete(f'/api/v1/accounts/{self.admin.id}/workstations', headers=self.account_headers())
        self.assertEqual(response.json()['workstations'], 0)


if __name__ == '__main__':
    unittest.main()
