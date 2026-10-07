"""Notification setup must never replace the main panel state or draft."""
from dataclasses import replace
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey
from telegram_client.routers.common import NotificationStateMiddleware
from telegram_client.routers import admin_requests as requests, admin_profiles as profiles
from telegram_client.backend import BackendError


class RequestSetupTests(IsolatedAsyncioTestCase):
    async def test_saved_notification_profile_without_grants_has_nonempty_rich_access_block(self):
        self.backend.profile_grants.return_value = {'items': []}
        for locale in ('en', 'ru'):
            await self.notice.update_data(locale=locale, notification_session=True,
                profile_setup={'profile_id': 'p1'})
            with patch.object(profiles, 'render', new_callable=AsyncMock) as draw:
                await profiles.show_setup_overview(123, 123, 99, self.bot, self.backend, self.notice)
            screen, rows = draw.call_args.args[2:4]
            details = next(block for block in screen.rich(rows).blocks if block.type == 'details')
            self.assertEqual(details.blocks[0].text, profiles.tr(locale, 'account.access_empty'))
            self.assertEqual([button.callback_data for row in rows for button in row],
                             ['setup_edit', 'notification_close'])
            await self.assert_main_unchanged()

    async def asyncSetUp(self):
        self.main = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=123, user_id=123))
        await self.main.set_state('main:wizard')
        await self.main.update_data(locale='en', control_message_id=77, draft_grants=[{'node_key': 'original', 'protocol': 'awg'}])
        self.original = await self.main.get_data()
        self.query = SimpleNamespace(data='notification_decide:request:approve', answer=AsyncMock(),
            from_user=SimpleNamespace(id=123, language_code='en'),
            message=SimpleNamespace(message_id=99, chat=SimpleNamespace(id=123)))
        self.notice = FSMContext(self.main.storage, replace(self.main.key, destiny='notification:99'))
        self.bot = SimpleNamespace(delete_message=AsyncMock())
        self.profile = {'id': 'p1', 'display_name': 'Alice', 'desired_revision': 4,
            'owner_account_id': 'member', 'frozen': False, 'expires_at': None}
        self.owner = {'id': 'member', 'role': 'member', 'status': 'approved', 'telegram_user_id': 456}
        async def request(method, path, **kwargs):
            if path.endswith('/decision'):
                return {'account_id': 'member'}
            if '/accounts/' in path:
                return self.owner
            return self.profile
        self.backend = SimpleNamespace(request=AsyncMock(side_effect=request),
            pending_access_request=AsyncMock(return_value={'locale': 'en'}),
            profiles=AsyncMock(return_value={'items': [self.profile]}),
            profile_grants=AsyncMock(return_value={'items': [{'node_key': 'n1', 'protocol': 'awg'}]}),
            admin_nodes=AsyncMock(return_value={'items': [{'key': 'n1', 'title': 'Latvia', 'region': 'EU', 'protocols': ['awg', 'xray']}]}),
            edit_profile=AsyncMock())

    async def dispatch(self, callback, data=None):
        async def handle(event, context):
            return await callback(context['state'])
        return await NotificationStateMiddleware()(handle, SimpleNamespace(callback_query=self.query, message=None),
            {'state': self.main, 'raw_state': await self.main.get_state(), **(data or {})})

    async def assert_main_unchanged(self):
        self.assertEqual(await self.main.get_data(), self.original)
        self.assertEqual(await self.main.get_state(), 'main:wizard')

    async def test_approval_setup_save_edit_close_preserves_original_panel(self):
        async def draw(bot, chat, screen, rows, state, message_id=None):
            await state.update_data(control_message_id=message_id)
        # Recipient delivery uses its default FSM, not the notice destiny.
        with patch.object(requests, 'render', side_effect=draw) as result_draw, patch.object(profiles, 'render', side_effect=draw):
            await self.dispatch(lambda state: requests.apply_decision(self.query, 'request', 'approve', self.bot, self.backend, state, notification=True))
            self.bot.delete_message.assert_not_awaited()
            callbacks = [b.callback_data for row in result_draw.call_args_list[0].args[3] for b in row]
            self.assertEqual(callbacks, ['request_profile:request', 'notification_close'])
            recipient = FSMContext(self.main.storage, replace(self.main.key, user_id=456, chat_id=456))
            self.assertEqual((await recipient.get_data())['locale'], 'en')
            self.query.data = 'request_profile:request'
            await self.dispatch(lambda state: requests.request_profile_cb(self.query, self.bot, self.backend, state))
            self.assertEqual((await self.notice.get_data())['edit_profile_revision'], 4)
            self.query.data = 'setup_time'
            await self.dispatch(lambda state: profiles.setup_time_cb(self.query, self.bot, state))
            nonce = (await self.notice.get_data())['setup_time_nonce']
            self.query.data = f'setup_exp:{nonce}:30'
            await self.dispatch(lambda state: profiles.setup_exp_cb(self.query, self.bot, state))
            draft = (await self.notice.get_data())['profile_setup']
            self.query.data = 'setup_save'
            await self.dispatch(lambda state: profiles.setup_save_cb(self.query, self.bot, self.backend, state))
            self.backend.edit_profile.assert_awaited_once_with(123, 'p1', 4,
                {'expires_at': draft['expires_at'], 'grants': [{'node_key': 'n1', 'protocol': 'awg'}]}, command_key=draft['command_key'])
            await self.assert_main_unchanged()
            self.query.data = 'setup_edit'
            await self.dispatch(lambda state: profiles.setup_edit_cb(self.query, self.bot, self.backend, state))
            self.query.data = 'notification_close'
            await self.dispatch(lambda state: requests.notification_close_cb(self.query, self.bot, state))
            self.bot.delete_message.assert_awaited_once_with(123, 99)
            await self.assert_main_unchanged()
            self.query.data = 'setup_save'
            self.backend.edit_profile.reset_mock()
            await self.dispatch(lambda state: profiles.setup_save_cb(self.query, self.bot, self.backend, state))
            self.backend.edit_profile.assert_not_awaited()

    async def test_rejection_has_close_only_and_does_not_clear_main(self):
        with patch.object(requests, 'render', new_callable=AsyncMock) as draw:
            await self.dispatch(lambda state: requests.apply_decision(self.query, 'request', 'reject', self.bot, self.backend, state, notification=True))
        rows = draw.call_args_list[0].args[3]
        self.assertEqual([b.callback_data for row in rows for b in row], ['notification_close'])
        await self.assert_main_unchanged()

    async def test_reply_input_routes_only_to_its_notification(self):
        await self.notice.update_data(notification_session=True)
        await self.notice.set_state(profiles.SetupDateState.waiting_for_date)
        async def handler(event, data):
            self.assertEqual(data['state'].key, self.notice.key)
            self.assertEqual(data['raw_state'], await self.notice.get_state())
        event = SimpleNamespace(callback_query=None, message=SimpleNamespace(reply_to_message=self.query.message))
        await NotificationStateMiddleware()(handler, event, {'state': self.main})
        await self.assert_main_unchanged()

    async def test_failed_save_retains_draft_and_revision(self):
        await self.notice.update_data(notification_session=True, locale='ru', control_message_id=99)
        with patch.object(profiles, 'render', new_callable=AsyncMock):
            await profiles.start_profile_setup(123, 123, 99, 'p1', self.bot, self.backend, self.notice)
        draft = await self.notice.get_data()
        self.backend.edit_profile.side_effect = BackendError('revision_conflict', 412)
        await profiles.setup_save_cb(self.query, self.bot, self.backend, self.notice)
        self.assertEqual(await self.notice.get_data(), draft)
        self.query.answer.assert_awaited_with(profiles.tr('ru', 'profile.admin.error_changed'), show_alert=True)
        await self.assert_main_unchanged()

    async def test_admin_time_screen_has_no_finite_choices(self):
        self.owner['role'] = 'admin'
        await self.notice.update_data(notification_session=True, locale='en')
        with patch.object(profiles, 'render', new_callable=AsyncMock) as draw:
            await profiles.start_profile_setup(123, 123, 99, 'p1', self.bot, self.backend, self.notice)
            await profiles.setup_time_cb(self.query, self.bot, self.notice)
        screen, rows = draw.call_args.args[2:4]
        self.assertIn('Administrators always have permanent access.', screen.plain())
        self.assertEqual(len(rows), 1)
        self.assertEqual([b.style for b in rows[0]], [None, 'primary'])
        self.assertFalse(any(b.callback_data.startswith('setup_exp:') for row in rows for b in row))


    async def test_concurrent_change_while_opening_setup_does_not_capture_mixed_revision(self):
        self.backend.request.side_effect = [self.profile, self.owner, {**self.profile, 'desired_revision': 5}]
        with self.assertRaises(BackendError) as failed:
            await profiles.start_profile_setup(123, 123, 99, 'p1', self.bot, self.backend, self.notice)
        self.assertEqual(failed.exception.status, 412)
        self.assertNotIn('profile_setup', await self.notice.get_data())
        await self.assert_main_unchanged()

    async def test_recreated_notification_keeps_its_isolated_workflow(self):
        from telegram_client.routers.common import render
        from telegram_client.screens import Screen
        from aiogram.exceptions import TelegramBadRequest
        from aiogram.methods import EditMessageText
        error = TelegramBadRequest(method=EditMessageText(chat_id=123, message_id=99, text='text'), message='message to edit not found')
        bot = SimpleNamespace(edit_message_text=AsyncMock(side_effect=error),
            send_rich_message=AsyncMock(return_value=SimpleNamespace(message_id=100)))
        await self.notice.set_state(profiles.SetupDateState.waiting_for_date)
        await self.notice.update_data(notification_session=True, control_message_id=99, profile_setup={'profile_id': 'p1'})
        await render(bot, 123, Screen('Date'), [], self.notice, 99)
        replacement = FSMContext(self.main.storage, replace(self.main.key, destiny='notification:100'))
        self.assertEqual((await replacement.get_data())['profile_setup']['profile_id'], 'p1')
        self.assertEqual(await replacement.get_state(), await self.notice.get_state())
        self.assertTrue((await self.notice.get_data())['notification_closed'])
        await self.assert_main_unchanged()
