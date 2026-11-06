"""Merging accounts in the bot: a member's code and request, an administrator's decision."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.navigation import screen_parents
from telegram_client.routers import account_merges as merges
from tests import test_telegram_admin_profiles_rich as fixture

REQUEST = 'c' * 32


def preview(vpn='choose', keep_main_only=False):
    account = {'id': 'x', 'login': 'nora', 'vpn': {'devices': 2, 'tariff': 'Month'}}
    return {'main': account, 'other': {**account, 'login': None, 'username': 'nora_tg'},
            'result': {'vpn': vpn, 'telegram': 'moves', 'password': None, 'login': None, 'passkeys_removed': 1,
                       'keep_main_only': keep_main_only}}


class Backend:
    def __init__(self, items=()):
        self.calls, self.items = [], list(items)

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs.get('body')))
        if path == '/api/v1/me/merge-code':
            return {'code': 'ABCD-EFGH'}
        if path in {'/api/v1/me/merge-requests', '/api/v1/merge-requests'} and method == 'GET':
            return {'items': self.items}
        if path == '/api/v1/merge-requests/' + REQUEST:
            return {'id': REQUEST, 'status': 'pending', 'preview': preview()}
        return {}


class MergeTests(IsolatedAsyncioTestCase):
    setUp = fixture.ProfileRichTests.setUp

    async def press(self, data, backend, handler=merges.member_cb):
        self.query.data = data
        with patch.object(merges, 'render', new_callable=AsyncMock) as draw:
            await handler(self.query, self.bot, backend, self.state)
        return draw.call_args.args[2:4]

    def test_describe(self):
        lines = merges.describe('en', preview())
        self.assertIn('Main: nora', lines)
        self.assertIn('Joins: @nora_tg', lines)
        self.assertIn('Both have a VPN profile: one stays, the other is deleted.', lines)
        self.assertIn('1 passkeys of the other account are removed.', lines)

    async def test_a_member_gets_and_enters_a_code(self):
        backend = Backend()
        screen, rows = await self.press('merge:view', backend)
        self.assertEqual([row[0].callback_data for row in rows[:2]], ['merge:code', 'merge:enter'])
        screen, _ = await self.press('merge:code', backend)
        self.assertIn('Code: ABCD-EFGH', screen.plain())
        await self.press('merge:enter', backend)
        message = SimpleNamespace(chat=SimpleNamespace(id=123, type='private'), from_user=SimpleNamespace(id=123),
                                  text=' abcd-efgh ', delete=AsyncMock())
        with patch.object(merges, 'render', new_callable=AsyncMock):
            await merges.code_text(message, self.bot, backend, self.state)
        self.assertIn(('POST', '/api/v1/me/merge-requests', {'code': 'abcd-efgh'}), backend.calls)
        backend.items = [{'id': REQUEST, 'status': 'pending', 'preview': preview()}]
        screen, rows = await self.press('merge:view', backend)
        self.assertIn('A merge waits for an administrator:', screen.plain())
        self.assertEqual(rows[0][0].callback_data, 'merge:cancel:' + REQUEST)

    async def test_an_administrator_chooses_the_vpn_and_merges(self):
        backend = Backend([{'id': REQUEST, 'status': 'pending', 'preview': preview()}])
        screen, rows = await self.press('amerge:list', backend, merges.admin_cb)
        self.assertEqual(rows[0][0].callback_data, 'amerge:' + REQUEST)
        self.assertEqual([c.label for c in screen_parents(screen, rows, 'en', self.data)][-1], 'Accounts')
        screen, rows = await self.press('amerge:' + REQUEST, backend, merges.admin_cb)
        self.assertEqual([row[0].callback_data for row in rows[:3]],
                         [f'amerge:ok:{REQUEST}:m', f'amerge:ok:{REQUEST}:o', 'amerge:no:' + REQUEST])
        self.assertTrue(all(len(row[0].callback_data) <= 64 for row in rows))
        screen, _ = await self.press(f'amerge:ok:{REQUEST}:o', backend, merges.admin_cb)
        self.assertIn(('POST', f'/api/v1/merge-requests/{REQUEST}/decision',
                       {'decision': 'approve', 'keep_profile': 'other'}), backend.calls)
        self.assertIn('Accounts merged.', screen.plain())
