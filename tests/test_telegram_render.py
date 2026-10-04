"""Recover a single control screen when its old message or Rich API is unavailable."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest, TelegramNotFound, TelegramNetworkError
from aiogram.methods import EditMessageText, SendRichMessage
from telegram_client.routers.common import render
from telegram_client.screens import Screen
from telegram_client.routers import user


class RenderRecoveryTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.data = {'control_message_id': 10}
        async def get_data():
            return dict(self.data)
        async def update_data(**values):
            self.data.update(values)
        self.state = SimpleNamespace(get_data=get_data, update_data=update_data, set_state=AsyncMock())
        self.bot = SimpleNamespace(edit_message_text=AsyncMock(),
            send_rich_message=AsyncMock(return_value=SimpleNamespace(message_id=20)),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=30)))

    async def test_deleted_screen_is_recreated_for_both_telegram_error_codes(self):
        for error in (TelegramBadRequest, TelegramNotFound):
            with self.subTest(error=error):
                self.bot.edit_message_text.side_effect = error(
                    method=EditMessageText(chat_id=1, message_id=10, text='old'),
                    message='message to edit not found')
                await render(self.bot, 1, Screen('ID', ('101',)), [], self.state)
                self.assertEqual(self.data['control_message_id'], 20)

    async def test_unsupported_rich_send_falls_back_to_plain_and_records_new_id(self):
        self.data.clear()
        self.bot.send_rich_message.side_effect = TelegramNotFound(
            method=SendRichMessage(chat_id=1, rich_message=Screen('ID').rich()), message='Not Found')
        await render(self.bot, 1, Screen('ID', ('101',)), [], self.state)
        self.assertEqual(self.data['control_message_id'], 30)
        self.assertEqual(self.bot.send_message.call_args.kwargs['text'], 'ID\n\n101')

    async def test_rich_network_timeout_does_not_leave_command_without_a_screen(self):
        self.data.clear()
        self.bot.send_rich_message.side_effect = TelegramNetworkError(
            method=SendRichMessage(chat_id=1, rich_message=Screen('Version').rich()), message='timeout')
        await render(self.bot, 1, Screen('Version'), [], self.state)
        self.assertEqual(self.bot.send_rich_message.call_args.kwargs['request_timeout'], 10)
        self.assertEqual(self.data['control_message_id'], 30)

    async def test_unchanged_message_does_not_send_a_duplicate(self):
        self.bot.edit_message_text.side_effect = TelegramBadRequest(
            method=EditMessageText(chat_id=1, message_id=10, text='old'), message='message is not modified')
        await render(self.bot, 1, Screen('ID'), [], self.state)
        self.bot.send_rich_message.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()

    async def test_commands_recreate_deleted_control_screen(self):
        self.bot.delete_message = AsyncMock()
        self.bot.edit_message_text.side_effect = TelegramBadRequest(
            method=EditMessageText(chat_id=1, message_id=10, text='old'), message='message to edit not found')
        message = SimpleNamespace(chat=SimpleNamespace(id=1, type='private'), delete=AsyncMock(),
            from_user=SimpleNamespace(id=101, username='admin', first_name='Admin',
                last_name=None, language_code='en'))
        backend = SimpleNamespace(resolve=AsyncMock(),
            me=AsyncMock(return_value={'role': 'admin', 'status': 'approved', 'locale': 'en', 'locale_selected': True}),
            bot_title=AsyncMock(return_value={'title': 'Node Plane'}),
            system_version=AsyncMock(return_value={'version': '0.4.3-alpha.22'}))
        for command in (user.start_cmd, user.id_cmd, user.version_cmd):
            with self.subTest(command=command.__name__):
                self.data['control_message_id'] = 10
                before = self.bot.send_rich_message.await_count
                await command(message, self.bot, backend, self.state)
                self.assertEqual(self.bot.send_rich_message.await_count, before + 1)
                self.assertEqual(self.data['control_message_id'], 20)
