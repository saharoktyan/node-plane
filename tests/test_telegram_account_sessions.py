"""Passwords and sessions in the bot: no password entry, the member's sessions, an administrator's sign-out."""
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.navigation import screen_parents
from telegram_client.routers import account_sessions as sessions
from tests import test_telegram_admin_profiles_rich as fixture

ACCOUNT = 'a1b2c3d4-0000-4000-8000-000000000001'
ITEMS = [{'id': 's1', 'device': 'Firefox', 'used_at': '2026-11-06T10:00:00+00:00', 'current': False},
         {'id': 's2', 'device': 'Phone', 'used_at': '2026-11-05T09:00:00+00:00', 'current': False}]


class Backend:
    def __init__(self):
        self.calls = []

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path))
        if path.endswith('/passkeys') and method == 'GET':
            return {'items': [{'id': 'k' * 43, 'name': 'Mac', 'created_at': '2026-11-01T08:00:00+00:00'}]}
        return {'items': ITEMS} if method == 'GET' else {}


class SessionTests(IsolatedAsyncioTestCase):
    setUp = fixture.ProfileRichTests.setUp

    async def press(self, data, backend, handler=sessions.sessions_cb):
        self.query.data = data
        with patch.object(sessions, 'render', new_callable=AsyncMock) as draw, \
                patch('telegram_client.routers.admin_profiles.show_account', new_callable=AsyncMock) as card:
            await handler(self.query, self.bot, backend, self.state)
        return (draw.call_args.args[2:4] if draw.called else None), card.called

    def test_settings_and_card(self):
        section = sessions.settings_section('en', 123, {'has_password': False})
        self.assertIn('not entered in Telegram', section.lines[0])
        self.assertEqual([b.callback_data for row in section.rows for b in row], ['msess:tglink', 'merge:view'])
        # Passkeys alone also give sessions.
        self.assertEqual(len(sessions.settings_section('en', 123, {'passkeys': 1}).rows), 3)
        section = sessions.settings_section('en', 123, {'has_password': True})
        self.assertEqual([b.callback_data for row in section.rows for b in row], ['msess:list', 'msess:tglink', 'merge:view'])
        account = {'id': ACCOUNT, 'has_password': True, 'sessions': 2, 'passkeys': 1}
        self.assertEqual(sessions.card_lines('en', account), ('Password: set', 'Sessions: 2', 'Passkeys: 1'))
        self.assertEqual([row[0].callback_data for row in sessions.card_rows('en', account)],
                         ['asess:' + ACCOUNT, 'akeys:' + ACCOUNT])
        self.assertEqual(sessions.card_rows('en', {**account, 'sessions': 0, 'passkeys': 0}), [])

    async def test_member_lists_and_ends_sessions(self):
        backend = Backend()
        (screen, rows), _ = await self.press('msess:list', backend)
        self.assertIn('Firefox — last active 2026-11-06 10:00', screen.plain())
        self.assertIn('Mac — added 2026-11-01 08:00', screen.plain())
        self.assertEqual([row[0].callback_data for row in rows[:4]],
                         ['msess:end:s1', 'msess:end:s2', 'msess:all', 'msess:key:0'])
        self.assertEqual([c.label for c in screen_parents(screen, rows, 'en', self.data)][-1], 'Settings')
        await self.press('msess:end:s2', backend)
        self.assertIn(('DELETE', '/api/v1/me/sessions/s2'), backend.calls)
        await self.press('msess:all', backend)
        self.assertIn(('DELETE', '/api/v1/me/sessions'), backend.calls)
        await self.press('msess:key:0', backend)
        self.assertIn(('DELETE', '/api/v1/me/passkeys/' + 'k' * 43), backend.calls)

    async def test_the_bot_gives_a_code_to_move_its_telegram(self):
        class CodeBackend(Backend):
            def __init__(self, error=None):
                super().__init__()
                self.error = error

            async def request(self, method, path, **kwargs):
                self.calls.append((method, path))
                if self.error:
                    from telegram_client.backend import BackendError
                    raise BackendError(self.error, 409)
                return {'code': 'ABCD-EFGH', 'expires_at': '2026-11-06T12:10:00+00:00'}
        backend = CodeBackend()
        (screen, _), _ = await self.press('msess:tglink', backend)
        self.assertEqual(backend.calls, [('POST', '/api/v1/me/telegram/link-code')])
        self.assertIn('Code: ABCD-EFGH', screen.plain())
        (screen, _), _ = await self.press('msess:tglink', CodeBackend('telegram_account_has_access'))
        self.assertIn('has access to services', screen.plain())

    async def test_administrator_ends_an_accounts_sessions(self):
        backend = Backend()
        _, card = await self.press('asess:' + ACCOUNT, backend, sessions.account_sessions_cb)
        self.assertTrue(card)
        self.assertEqual(backend.calls, [('DELETE', f'/api/v1/accounts/{ACCOUNT}/sessions')])
        (screen, rows), card = await self.press('akeys:' + ACCOUNT, backend, sessions.account_sessions_cb)
        self.assertFalse(card)
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(rows[0][0].callback_data, 'akeysok:' + ACCOUNT)
        _, card = await self.press('akeysok:' + ACCOUNT, backend, sessions.account_sessions_cb)
        self.assertEqual((card, backend.calls[-1]), (True, ('DELETE', f'/api/v1/accounts/{ACCOUNT}/passkeys')))
