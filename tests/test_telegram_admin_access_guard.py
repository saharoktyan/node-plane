from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from telegram_client.access_guard import AdminAccessMiddleware
from telegram_client.backend import BackendError
from telegram_client.routers.navigation import NavigationGuardMiddleware
from telegram_client.routers import user


class AdminAccessGuardTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=123, user_id=123))
        await self.state.set_state('NodeEditState:waiting')
        await self.state.update_data(locale='en', control_message_id=77,
            navigation_screen={'message_id': 77, 'nonce': 'old', 'parents': [{'callback': 'admin_menu'}]},
            node_settings_draft={'values': {'host': 'new'}, 'baseline': {'host': 'old'}})
        self.account = {'role': 'member', 'status': 'approved'}
        self.backend = SimpleNamespace(me=AsyncMock(return_value=self.account))
        self.bot = SimpleNamespace(delete_message=AsyncMock())
        self.query = SimpleNamespace(data='admin_menu', answer=AsyncMock(),
            from_user=SimpleNamespace(id=123),
            message=SimpleNamespace(chat=SimpleNamespace(id=123), message_id=77))
        self.data = {'state': self.state, 'backend': self.backend, 'bot': self.bot}

    async def test_back_after_demotion_bypasses_discard_and_clears_admin_fsm(self):
        destination = AsyncMock()
        async def navigate(event, data):
            return await NavigationGuardMiddleware()(destination, event, data)
        with patch.object(user, 'show_home', new_callable=AsyncMock) as home:
            await AdminAccessMiddleware()(navigate, SimpleNamespace(callback_query=self.query), self.data)
        home.assert_awaited_once()
        destination.assert_not_awaited()
        self.query.answer.assert_awaited_once()
        self.assertIsNone(await self.state.get_state())
        self.assertNotIn('navigation_discard', await self.state.get_data())
        self.query.data = user.button(123, 'Home', 'home').callback_data
        await AdminAccessMiddleware()(destination, SimpleNamespace(callback_query=self.query), self.data)
        destination.assert_awaited_once()

    async def test_old_admin_button_rechecks_role_after_bot_restart(self):
        await self.state.clear()
        self.query.data = 'updates'
        handler = AsyncMock()
        with patch.object(user, 'show_home', new_callable=AsyncMock) as home:
            await AdminAccessMiddleware()(handler, SimpleNamespace(callback_query=self.query), self.data)
        home.assert_awaited_once()
        handler.assert_not_awaited()

    async def test_admin_text_input_is_abandoned_after_demotion(self):
        message = SimpleNamespace(from_user=self.query.from_user, chat=self.query.message.chat, message_id=90)
        handler = AsyncMock()
        with patch.object(user, 'show_home', new_callable=AsyncMock) as home:
            await AdminAccessMiddleware()(handler, SimpleNamespace(message=message), self.data)
        home.assert_awaited_once()
        # The existing control panel is replaced; the user's text is not edited.
        self.assertEqual(home.call_args.args[5], 77)
        handler.assert_not_awaited()

    async def test_authorized_admin_still_reaches_handler(self):
        self.account['role'] = 'admin'
        handler = AsyncMock()
        await AdminAccessMiddleware()(handler, SimpleNamespace(callback_query=self.query), self.data)
        handler.assert_awaited_once()
        self.assertIsNotNone(await self.state.get_state())

    async def test_revocation_between_check_and_handler_recovers_home(self):
        self.backend.me.side_effect = [{'role': 'admin', 'status': 'approved'}, self.account]
        handler = AsyncMock(side_effect=BackendError('permission_denied', 403))
        with patch.object(user, 'show_home', new_callable=AsyncMock) as home:
            await AdminAccessMiddleware()(handler, SimpleNamespace(callback_query=self.query), self.data)
        home.assert_awaited_once()

    async def test_backend_outage_does_not_erase_admin_draft(self):
        self.backend.me.side_effect = BackendError('backend_unavailable', 503)
        with self.assertRaises(BackendError):
            await AdminAccessMiddleware()(AsyncMock(), SimpleNamespace(callback_query=self.query), self.data)
        self.assertIn('node_settings_draft', await self.state.get_data())

    async def test_cached_home_back_from_admin_menu_rechecks_the_current_role(self):
        self.query.data = user.button(123, 'Back', 'home').callback_data
        await self.state.set_data({'admin_panel_active': True, 'control_message_id': 77,
            'home_presentation': {'account': {'role': 'admin', 'status': 'approved'}},
            'navigation_screen': {'parents': [{'callback': self.query.data}]}})
        handler = AsyncMock()
        with patch.object(user, 'show_home', new_callable=AsyncMock) as home:
            await AdminAccessMiddleware()(handler, SimpleNamespace(callback_query=self.query), self.data)
        home.assert_awaited_once()
        handler.assert_not_awaited()
        self.assertNotIn('home_presentation', await self.state.get_data())
