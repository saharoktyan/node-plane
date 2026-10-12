"""Refresh an open menu after account changes without disturbing other screens."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from telegram_client.announcement_delivery import refresh_menu


class MenuRefreshTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dispatcher = SimpleNamespace(storage=MemoryStorage(), fsm=SimpleNamespace(events_isolation=SimpleEventIsolation()))
        self.state = FSMContext(self.dispatcher.storage, StorageKey(bot_id=1, chat_id=102, user_id=102))
        self.account = {'id': 'account', 'role': 'admin', 'status': 'approved', 'telegram_user_id': 102}
        self.backend = SimpleNamespace(request=AsyncMock(side_effect=[{'delivery': {'account': self.account, 'revision': 3}}, {'ok': True}]))
        self.bot = SimpleNamespace(id=1)

    async def test_open_main_menu_is_refreshed_with_new_role(self):
        await self.state.update_data(control_message_id=77, home_presentation={'account': {'role': 'member'}},
            navigation_screen={'parents': [{'callback': 'u:home'}]})
        with patch('telegram_client.routers.user.show_home', new_callable=AsyncMock) as show:
            self.assertTrue(await refresh_menu(self.bot, self.backend, self.dispatcher))
        show.assert_awaited_once()
        self.assertEqual(show.call_args.args[0:2], (102, 102))
        self.assertEqual(show.call_args.args[5], 77)
        self.assertEqual(show.call_args.kwargs['account']['role'], 'admin')
        self.assertIsNone((await self.state.get_data())['home_presentation'])
        self.assertIn('revision=3', self.backend.request.call_args.args[1])

    async def test_other_screen_is_preserved_but_cached_home_is_invalidated(self):
        await self.state.update_data(control_message_id=77, home_presentation={'account': {'role': 'member'}},
            navigation_screen={'parents': [{'callback': 'u:home'}, {'callback': 'u:profiles'}]}, draft='keep')
        with patch('telegram_client.routers.user.show_home', new_callable=AsyncMock) as show:
            self.assertTrue(await refresh_menu(self.bot, self.backend, self.dispatcher))
        show.assert_not_awaited()
        data = await self.state.get_data()
        self.assertIsNone(data['home_presentation'])
        self.assertEqual(data['draft'], 'keep')
