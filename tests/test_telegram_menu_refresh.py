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

    async def test_revoked_admin_is_ejected_from_every_admin_level_and_draft(self):
        self.account.update(role='member')
        for parents in ([{'callback': 'admin_menu'}],
                [{'callback': 'admin_menu'}, {'callback': 'admin_nodes'}, {'callback': 'node_settings:n1'}]):
            with self.subTest(parents=parents):
                self.backend.request.side_effect = [{'delivery': {'account': self.account, 'revision': 3}}, {'ok': True}]
                await self.state.set_state('NodeEditState:waiting')
                await self.state.set_data({'control_message_id': 77, 'locale': 'ru',
                    'home_presentation': {'account': {'role': 'admin'}},
                    'navigation_screen': {'parents': parents}, 'node_settings_draft': {'secret': 'discard'}})
                with patch('telegram_client.routers.user.show_home', new_callable=AsyncMock) as show:
                    await refresh_menu(self.bot, self.backend, self.dispatcher)
                show.assert_awaited_once()
                self.assertEqual(show.call_args.kwargs['account']['role'], 'member')
                values = await self.state.get_data()
                self.assertNotIn('node_settings_draft', values)
                self.assertNotIn('navigation_screen', values)
                self.assertIsNone(await self.state.get_state())
                self.assertEqual(values['locale'], 'ru')

    async def test_failed_ejection_is_not_acknowledged_and_can_be_retried(self):
        self.account.update(role='member')
        await self.state.update_data(control_message_id=77,
            navigation_screen={'parents': [{'callback': 'admin_menu'}]})
        with patch('telegram_client.routers.user.show_home', new_callable=AsyncMock,
                side_effect=RuntimeError('temporary transport failure')):
            with self.assertRaises(RuntimeError):
                await refresh_menu(self.bot, self.backend, self.dispatcher)
        self.assertEqual(self.backend.request.await_count, 1)
        self.assertTrue((await self.state.get_data())['account_menu_recovery_pending'])
        self.backend.request.side_effect = [{'delivery': {'account': self.account, 'revision': 3}}, {'ok': True}]
        with patch('telegram_client.routers.user.show_home', new_callable=AsyncMock) as show:
            await refresh_menu(self.bot, self.backend, self.dispatcher)
        show.assert_awaited_once()
        self.assertFalse((await self.state.get_data())['account_menu_recovery_pending'])
        self.assertEqual(self.backend.request.await_count, 3)

    async def test_real_main_menu_switches_role_buttons_without_start_or_breadcrumbs(self):
        from telegram_client.routers import user
        from telegram_client.screens import Screen
        self.bot.edit_message_text = AsyncMock()
        self.bot.delete_message = AsyncMock()
        self.backend.bot_title = AsyncMock(return_value={'title': 'Node Plane'})
        self.backend.notifications = AsyncMock(return_value={'total': 0, 'items': []})
        def actions_in_render():
            rich = self.bot.edit_message_text.call_args.kwargs['rich_message']
            callbacks = []
            def visit(value):
                if isinstance(value, dict):
                    if str(value.get('callback_data', '')).startswith('u:'):
                        callbacks.append(user.actions[value['callback_data'][2:]].name)
                    for child in value.values():
                        visit(child)
                elif isinstance(value, list):
                    for child in value:
                        visit(child)
            visit(rich.model_dump())
            return set(callbacks)
        await self.state.update_data(locale='en', control_message_id=77)
        await user.show_home(102, 102, self.bot, self.backend, self.state, 77,
            account={**self.account, 'role': 'member'})
        self.assertNotIn('admin_menu', actions_in_render())
        self.assertIsNone((await self.state.get_data())['navigation_screen'])
        for role in ('admin', 'member', 'admin'):
            with self.subTest(role=role):
                self.account['role'] = role
                self.backend.request.side_effect = [{'delivery': {'account': self.account, 'revision': 3}}, {'ok': True}]
                await refresh_menu(self.bot, self.backend, self.dispatcher)
                self.assertEqual('admin_menu' in actions_in_render(), role == 'admin')
                self.assertEqual('quick_start' in actions_in_render(), role == 'admin')
                self.assertEqual(self.bot.edit_message_text.call_args.kwargs['message_id'], 77)
                self.assertEqual((await self.state.get_data())['home_presentation']['account']['role'], role)
                self.assertTrue((await self.state.get_data())['home_panel_active'])
        # Leaving the home screen prevents promotion from interrupting profile work.
        await user.render(self.bot, 102, Screen('Profile'), [], self.state, 77)
        self.assertFalse((await self.state.get_data())['home_panel_active'])
        self.bot.edit_message_text.reset_mock()
        self.backend.request.side_effect = [{'delivery': {'account': self.account, 'revision': 3}}, {'ok': True}]
        await refresh_menu(self.bot, self.backend, self.dispatcher)
        self.bot.edit_message_text.assert_not_awaited()

    async def test_failed_promotion_refresh_remains_retryable_after_render_clears_marker(self):
        await self.state.update_data(control_message_id=77, home_panel_active=True)
        async def fail_during_render(*args, **kwargs):
            await self.state.update_data(home_panel_active=False)
            raise RuntimeError('temporary Telegram delivery failure')
        with patch('telegram_client.routers.user.show_home', side_effect=fail_during_render):
            with self.assertRaises(RuntimeError):
                await refresh_menu(self.bot, self.backend, self.dispatcher)
        self.assertEqual(self.backend.request.await_count, 1)
        self.assertTrue((await self.state.get_data())['home_panel_active'])
        self.backend.request.side_effect = [{'delivery': {'account': self.account, 'revision': 3}}, {'ok': True}]
        with patch('telegram_client.routers.user.show_home', new_callable=AsyncMock) as show:
            await refresh_menu(self.bot, self.backend, self.dispatcher)
        show.assert_awaited_once()
        self.assertEqual(self.backend.request.await_count, 3)
