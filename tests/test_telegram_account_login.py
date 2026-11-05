"""Logins in the bot: the member's own in Settings, an administrator's from the account card."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.backend import BackendError
from telegram_client.navigation import screen_parents
from telegram_client.routers import account_login as login
from tests import test_telegram_admin_profiles_rich as fixture

ACCOUNT = 'a1b2c3d4-0000-4000-8000-000000000001'


class Backend:
    def __init__(self):
        self.calls, self.me_value = [], {'status': 'approved', 'login': None, 'login_suggestion': 'carol_tg'}

    async def me(self, user_id):
        return self.me_value

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs.get('body'), kwargs.get('revision')))
        if method == 'GET':
            return {'id': ACCOUNT, 'revision': 4, 'login': 'old', 'role': 'member', 'status': 'approved'}
        if kwargs['body']['login'] == 'root':
            raise BackendError('login_reserved', 422)
        return {}


class LoginTests(IsolatedAsyncioTestCase):
    setUp = fixture.ProfileRichTests.setUp

    async def press(self, data, backend):
        self.query.data = data
        with patch.object(login, 'render', new_callable=AsyncMock) as draw, \
                patch.object(login, '_done', new_callable=AsyncMock) as done:
            await login.login_cb(self.query, self.bot, backend, self.state)
        return draw.call_args.args[2:4] if draw.called else None, done.called

    async def send(self, text, backend):
        message = SimpleNamespace(chat=SimpleNamespace(id=123, type='private'), from_user=SimpleNamespace(id=123),
                                  text=text, delete=AsyncMock())
        with patch.object(login, 'render', new_callable=AsyncMock) as draw, \
                patch.object(login, '_done', new_callable=AsyncMock) as done:
            await login.login_text(message, self.bot, backend, self.state)
        return draw.call_args.args[2] if draw.called else None, done.called

    def test_settings_section(self):
        section = login.settings_section('en', 123, {'login': None, 'login_suggestion': 'carol_tg'})
        self.assertEqual([b.callback_data for row in section.rows for b in row], ['mlogin:edit', 'mlogin:use'])
        section = login.settings_section('en', 123, {'login': 'Carol', 'login_change_available_at': '2026-12-06T00:00:00+00:00'})
        self.assertEqual(section.lines, ('Login: Carol', 'You can change it again after 2026-12-06.'))
        self.assertEqual(section.rows, ())

    async def test_member_sets_own_login_and_sees_errors(self):
        backend = Backend()
        await self.press('mlogin:edit', backend)
        screen, done = await self.send('root', backend)
        self.assertIn('reserved', screen.plain())
        self.assertFalse(done)
        _, done = await self.send(' Carol ', backend)
        self.assertTrue(done)
        self.assertEqual(backend.calls[-1][:3], ('PUT', '/api/v1/me/login', {'login': 'Carol'}))
        _, done = await self.press('mlogin:use', backend)
        self.assertEqual(backend.calls[-1][:3], ('PUT', '/api/v1/me/login', {'login': 'carol_tg'}))

    async def test_administrator_changes_a_login_from_the_card(self):
        backend = Backend()
        (screen, rows), _ = await self.press('alogin:' + ACCOUNT, backend)
        self.assertEqual(rows[-1][0].callback_data, 'account:' + ACCOUNT)
        self.assertEqual([c.label for c in screen_parents(screen, rows, 'en', self.data)][-2:],
                         ['Accounts', 'Account'])
        _, done = await self.send('carol-x', backend)
        self.assertTrue(done)
        self.assertEqual(backend.calls[-1], ('PATCH', f'/api/v1/accounts/{ACCOUNT}', {'login': 'carol-x'}, 4))
