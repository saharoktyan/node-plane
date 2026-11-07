"""Invites: a new account with a template, an account's own sign-in, the bot, limits and the API."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from backend import invites, logins, sessions
from backend.authorization import AccessDenied, Principal, PrincipalKind, resolve_actor
from backend.credentials import PERMISSIONS
from tests import test_backend_http as http_fixture

NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)
PASSWORD = 'correct horse battery'


class InviteTests(unittest.TestCase):
    setUp_http = http_fixture.BackendHTTPTests.setUp
    register = http_fixture.BackendHTTPTests.register
    provision = http_fixture.BackendHTTPTests.provision

    def setUp(self):
        self.setUp_http()
        invites.set_bot_username(self.db, self.admin_actor(), '@np_test_bot')

    def admin_actor(self):
        return resolve_actor(Principal('test', PrincipalKind.ACCOUNT, PERMISSIONS, account_id=self.admin.id),
                             self.identities)

    def code(self, call, *args, **kwargs):
        with self.assertRaises(AccessDenied) as caught:
            call(*args, **kwargs)
        return caught.exception.code

    def tariff(self, days=30):
        tariff_id = str(uuid4())
        with self.db.transaction() as conn:
            conn.execute('''INSERT INTO backend_tariffs(id, title, canonical, duration_days, created_at, updated_at)
                VALUES (?, 'Month', 'month', ?, ?, ?)''', (tariff_id, days, NOW.isoformat(), NOW.isoformat()))
        return tariff_id

    def profile(self, account_id):
        with self.db.connect() as conn:
            return conn.execute('SELECT * FROM backend_profiles WHERE owner_account_id=?', (account_id,)).fetchone()

    def test_new_and_existing_account_links_use_the_fragment_route(self):
        with patch('backend.web_cabinet.origin', return_value='https://cab.example.com'):
            for options in ({'vpn': True}, {'account_id': self.admin.id}):
                created = invites.create(self.db, self.admin_actor(), **options)
                link = urlsplit(created['cabinet_link'])
                self.assertEqual(link.path, '/invite')
                self.assertEqual(link.fragment, created['token'])
                self.assertNotIn(created['token'], link.path + link.query)
                preview = invites.preview(self.db, link.fragment, source='10.0.0.1')
                self.assertEqual(preview['kind'], created['kind'])

    def test_the_bots_username_is_needed_for_its_link(self):
        with self.db.transaction() as conn:
            conn.execute('DELETE FROM backend_system_settings WHERE key=?', (invites.BOT_SETTING,))
        self.assertEqual(self.code(invites.create, self.db, self.admin_actor(), vpn=True),
                         'telegram_bot_username_required')
        for bad in ('np', 'np_test', 'np test bot', 'x' * 40 + 'bot'):
            self.assertEqual(self.code(invites.set_bot_username, self.db, self.admin_actor(), bad),
                             'telegram_bot_username_invalid', bad)
        self.assertEqual(invites.set_bot_username(self.db, self.admin_actor(), ' @Np_Test_Bot ')['username'],
                         'Np_Test_Bot')
        created = invites.create(self.db, self.admin_actor(), vpn=True)
        self.assertEqual(created['telegram_link'], 'https://t.me/Np_Test_Bot?start=' + created['token'])
        # An account's sign-in is not for the bot.
        account = invites.create(self.db, self.admin_actor(), account_id=self.admin.id)
        self.assertIsNone(account['telegram_link'])
        # Without Telegram the bot's username is not needed.
        from backend.account_channels import set_enabled
        with self.db.transaction() as conn:
            conn.execute('DELETE FROM backend_system_settings WHERE key=?', (invites.BOT_SETTING,))
            set_enabled(conn, frozenset())
        self.assertIsNone(invites.create(self.db, self.admin_actor(), vpn=True)['telegram_link'])

    def test_a_new_account_with_vpn_and_a_tariff_signs_in(self):
        tariff = self.tariff()
        created = invites.create(self.db, self.admin_actor(), vpn=True, tariff_id=tariff, note='For Dana', now=NOW)
        token = created['token']
        self.assertTrue(invites.TOKEN.fullmatch(token) and len(token) <= 64)
        self.assertIsNone(created['cabinet_link'])
        preview = invites.preview(self.db, token, source='10.0.0.1', now=NOW)
        self.assertEqual((preview['kind'], preview['vpn'], preview['tariff']), ('new', True, 'Month'))
        self.assertEqual(self.code(invites.accept, self.db, token, source='10.0.0.1', now=NOW), 'login_required')
        result = invites.accept(self.db, token, login='Dana', password=PASSWORD, device='Phone',
                                source='10.0.0.1', now=NOW)
        account_id = result['account']['id']
        self.assertEqual((result['account']['login'], result['account']['role']), ('Dana', 'member'))
        profile = self.profile(account_id)
        self.assertEqual(profile['display_name'], 'Dana')
        self.assertIsNotNone(profile['expires_at'])
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT tariff_id FROM backend_profile_tariffs WHERE profile_id=?',
                                          (profile['id'],)).fetchone()['tariff_id'], tariff)
        self.credentials.authenticate('Bearer ' + result['session']['access_token'], now=NOW)
        sessions.sign_in(self.db, 'dana', PASSWORD, device='x', source='10.0.0.1', now=NOW)
        # Used once.
        self.assertEqual(self.code(invites.accept, self.db, token, login='Other', source='10.0.0.1', now=NOW),
                         'invite_invalid')
        self.assertEqual(invites.listed(self.db, self.admin_actor(), now=NOW)[0]['status'], 'used')

    def test_an_account_invite_sets_the_login_and_replaces_the_password(self):
        account_id = str(uuid4())
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_accounts(id, status) VALUES (?, 'approved')", (account_id,))
        self.assertEqual(self.code(invites.create, self.db, self.admin_actor(), account_id=account_id, vpn=True),
                         'invalid_input')
        token = invites.create(self.db, self.admin_actor(), account_id=account_id, now=NOW)['token']
        result = invites.accept(self.db, token, login='erin', password=PASSWORD, source='10.0.0.1', now=NOW)
        self.assertEqual(result['account']['id'], account_id)
        other = sessions.sign_in(self.db, 'erin', PASSWORD, device='x', source='10.0.0.1', now=NOW)
        # Recovery: a new invite stands in for the lost password and ends the other sessions.
        token = invites.create(self.db, self.admin_actor(), account_id=account_id, now=NOW)['token']
        self.assertEqual(invites.preview(self.db, token, source='10.0.0.1', now=NOW)['login'], 'erin')
        invites.accept(self.db, token, password='another password', source='10.0.0.1', now=NOW)
        self.assertEqual(self.code(sessions.refresh, self.db, other['refresh_token'], now=NOW), 'invalid_credentials')
        sessions.sign_in(self.db, 'erin', 'another password', device='x', source='10.0.0.1', now=NOW)

    def test_expiry_revocation_wrong_tokens_and_the_creators_rights(self):
        actor = self.admin_actor()
        expired = invites.create(self.db, actor, days=1, now=NOW)['token']
        self.assertEqual(self.code(invites.preview, self.db, expired, source='s', now=NOW + timedelta(days=1)),
                         'invite_invalid')
        revoked = invites.create(self.db, actor, now=NOW)
        invites.revoke(self.db, actor, revoked['id'], now=NOW)
        self.assertEqual(self.code(invites.accept, self.db, revoked['token'], login='x1x', source='s', now=NOW),
                         'invite_invalid')
        for _ in range(sessions.SOURCE_FAILURES - 2):
            self.code(invites.preview, self.db, 'inv_' + 'x' * 43, source='s', now=NOW)
        self.assertEqual(self.code(invites.preview, self.db, expired, source='s', now=NOW), 'sign_in_rate_limited')
        # A taken login leaves the invite unused.
        token = invites.create(self.db, actor, vpn=True, now=NOW)['token']
        with self.db.transaction() as conn:
            logins.claim(conn, self.admin.id, 'boss', 'admin', NOW)
        self.assertEqual(self.code(invites.accept, self.db, token, login='BOSS', source='t', now=NOW), 'login_taken')
        # The template is applied with the creator's rights as they are when the invite is used.
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_accounts SET status='disabled' WHERE id=?", (self.admin.id,))
        self.assertEqual(self.code(invites.accept, self.db, token, login='frank', source='t', now=NOW),
                         'invite_creator_unavailable')
        with self.db.connect() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM backend_logins WHERE key='frank'").fetchone())

    def test_the_bot_registers_a_telegram_visitor(self):
        actor = self.admin_actor()
        token = invites.create(self.db, actor, vpn=True)['token']
        self.register(303, username='gina_tg')
        adapter = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '303'}
        response = self.client.post('/api/v1/invites/accept/telegram', headers=adapter, json={'token': token})
        self.assertEqual(response.status_code, 201, response.text)
        account = response.json()['account']
        self.assertEqual(account['telegram_user_id'], 303)
        me = self.client.get('/api/v1/me', headers=adapter).json()
        self.assertEqual((me['status'], me['login_suggestion']), ('approved', 'gina_tg'))
        self.assertIn('vpn', [service if isinstance(service, str) else service.get('service')
                              for service in me['services']])
        again = invites.create(self.db, actor)['token']
        response = self.client.post('/api/v1/invites/accept/telegram', headers=adapter, json={'token': again})
        self.assertEqual(response.json()['error']['code'], 'invite_already_registered')
        own = invites.create(self.db, actor, account_id=account['id'])['token']
        self.register(304)
        response = self.client.post('/api/v1/invites/accept/telegram', json={'token': own},
                                    headers={**self.headers, 'X-Node-Plane-Telegram-User-ID': '304'})
        self.assertEqual(response.json()['error']['code'], 'invite_not_for_telegram')

    def test_api_creates_lists_revokes_and_accepts(self):
        from backend.workstation_cli import SCOPES
        _, token = self.credentials.issue(PrincipalKind.ACCOUNT, SCOPES, account_id=self.admin.id)
        admin = {'Authorization': 'Bearer ' + token}
        created = self.client.post('/api/v1/invites', headers=admin, json={'vpn': True, 'days': 3, 'note': 'Ivy'})
        self.assertEqual(created.status_code, 201, created.text)
        created = created.json()
        response = self.client.post('/api/v1/invites/preview', json={'token': created['token']})
        self.assertEqual(response.json()['kind'], 'new', response.text)
        response = self.client.post('/api/v1/invites/accept', json={'token': created['token'], 'login': 'ivy'})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()['account']['login'], 'ivy')
        bot = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101'}
        response = self.client.post('/api/v1/invites', headers=bot, json={'account_id': self.admin.id})
        self.assertEqual(response.json()['error']['code'], 'invite_not_through_adapter')
        listed = self.client.get('/api/v1/invites', headers=admin).json()['items']
        self.assertEqual((listed[0]['status'], listed[0]['note']), ('used', 'Ivy'))
        self.assertNotIn('token', listed[0])
        response = self.client.put('/api/v1/invites/telegram-bot', headers=admin, json={'username': 'other_bot'})
        self.assertEqual(response.json(), {'username': 'other_bot', 'telegram_enabled': True})
        self.assertEqual(self.client.get('/api/v1/invites/telegram-bot', headers=admin).json()['username'],
                         'other_bot')
        second = self.client.post('/api/v1/invites', headers=admin, json={}).json()
        self.assertTrue(second['telegram_link'].startswith('https://t.me/other_bot?start=inv_'))
        self.assertEqual(self.client.get(f"/api/v1/invites/{second['id']}", headers=admin).json()['status'], 'active')
        self.assertEqual(self.client.delete(f"/api/v1/invites/{second['id']}", headers=admin).json()['status'],
                         'revoked')


if __name__ == '__main__':
    unittest.main()
