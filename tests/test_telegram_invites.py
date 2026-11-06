"""Invites in the bot: an administrator invites from Accounts, a person registers by `/start inv_…`."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.backend import BackendError
from telegram_client.navigation import screen_parents
from telegram_client.routers import invites
from tests import test_telegram_admin_profiles_rich as fixture

TOKEN = 'inv_' + 'a' * 43
INVITE = {'id': 'f' * 32, 'kind': 'new', 'vpn': True, 'tariff_id': None, 'status': 'active',
          'expires_at': '2026-11-13T00:00:00+00:00', 'token': TOKEN, 'cabinet_link': None}


class Backend:
    def __init__(self, error=None):
        self.calls, self.error = [], error

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs.get('body')))
        if self.error:
            raise BackendError(self.error, 404)
        if path == '/api/v1/tariffs':
            return {'items': [{'id': 't' * 36, 'title': 'Month'}]}
        if path == '/api/v1/invites' and method == 'GET':
            return {'items': [INVITE, {**INVITE, 'id': 'e' * 32, 'status': 'used'}]}
        return INVITE

    async def me(self, user_id):
        return {'status': 'approved', 'login': None, 'login_suggestion': 'gina_tg'}


class InviteTests(IsolatedAsyncioTestCase):
    setUp = fixture.ProfileRichTests.setUp

    async def press(self, data, backend):
        self.query.data = data
        self.bot.me = AsyncMock(return_value=SimpleNamespace(username='np_bot'))
        with patch.object(invites, 'render', new_callable=AsyncMock) as draw:
            await invites.invites_cb(self.query, self.bot, backend, self.state)
        return draw.call_args.args[2:4]

    async def test_an_administrator_invites_with_a_template(self):
        backend = Backend()
        screen, rows = await self.press('inv:new', backend)
        self.assertEqual([row[0].callback_data for row in rows[:3]],
                         ['inv:make:none', 'inv:make:vpn', 'inv:make:t:' + 't' * 36])
        self.assertTrue(all(len(row[0].callback_data) <= 64 for row in rows))
        self.assertEqual([c.label for c in screen_parents(screen, rows, 'en', self.data)][-1], 'Accounts')
        screen, rows = await self.press('inv:make:t:' + 't' * 36, backend)
        self.assertEqual(backend.calls[-1], ('POST', '/api/v1/invites', {'vpn': True, 'tariff_id': 't' * 36}))
        self.assertIn('https://t.me/np_bot?start=' + TOKEN, screen.plain())
        self.assertEqual([c.label for c in screen_parents(screen, rows, 'en', self.data)][-2:], ['Accounts', 'Invites'])
        screen, rows = await self.press('inv:list', backend)
        # Only active invites are listed.
        self.assertEqual([row[0].callback_data for row in rows[:-1]], ['inv:revoke:' + 'f' * 32])
        screen, _ = await self.press('inv:revoke:' + 'f' * 32, backend)
        self.assertIn(('DELETE', '/api/v1/invites/' + 'f' * 32, None), backend.calls)
        self.assertIn('Invite revoked.', screen.plain())

    async def test_a_person_registers_by_the_link(self):
        message = SimpleNamespace(chat=SimpleNamespace(id=123, type='private'), text='/start ' + TOKEN,
                                  from_user=SimpleNamespace(id=303, language_code='en'))
        self.assertEqual(invites.token_of(message), TOKEN)
        self.assertIsNone(invites.token_of(SimpleNamespace(text='/start')))
        backend = Backend()
        with patch.object(invites, 'render', new_callable=AsyncMock) as draw:
            await invites.redeem(message, self.bot, backend, self.state, TOKEN)
        screen, rows = draw.call_args.args[2:4]
        self.assertEqual(backend.calls, [('POST', '/api/v1/invites/accept/telegram', {'token': TOKEN})])
        self.assertIn('Your account is ready', screen.plain())
        # The login can be set right away.
        self.assertIn('mlogin:use', [row[0].callback_data for row in rows])
        with patch.object(invites, 'render', new_callable=AsyncMock) as draw:
            await invites.redeem(message, self.bot, Backend('invite_invalid'), self.state, TOKEN)
        self.assertIn('used, revoked or expired', draw.call_args.args[2].plain())
