import asyncio
from pathlib import Path
import tempfile
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramConflictError, TelegramNetworkError
from aiogram.methods import GetMe, GetUpdates, SendMessage
from aiogram.types import User

from telegram_client.polling import BotTokenConflict, PollingConflictGuard, guarded_polling


class PollingGuardTests(IsolatedAsyncioTestCase):
    async def test_polling_conflict_stops_real_dispatcher_and_records_reason(self):
        bot = Bot('123456:unit-test-token')
        dispatcher = Dispatcher()
        def respond(bot, method, **kwargs):
            if isinstance(method, GetMe):
                return User(id=123456, is_bot=True, first_name='Test')
            raise TelegramConflictError(method=method, message='competing getUpdates')
        bot.session.make_request = AsyncMock(side_effect=respond)
        bot.session.close = AsyncMock()
        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory) / 'conflict.json'
            with self.assertRaises(BotTokenConflict):
                await asyncio.wait_for(guarded_polling(dispatcher, bot, status), timeout=3)
            self.assertIn('telegram_polling_conflict', status.read_text())
            self.assertNotIn(bot.token, status.read_text())
        self.assertFalse(dispatcher._running_lock.locked())
        self.assertEqual(len(bot.session.middleware), 0)
        self.assertEqual(sum(isinstance(call.args[1], GetUpdates)
                             for call in bot.session.make_request.await_args_list), 1)
        bot.session.close.assert_awaited()

    async def test_network_error_does_not_mark_a_token_conflict(self):
        guard = PollingConflictGuard()
        method = GetUpdates()
        request = AsyncMock(side_effect=TelegramNetworkError(method=method, message='offline'))
        with self.assertRaises(TelegramNetworkError):
            await guard(request, None, method)
        self.assertFalse(guard.conflict.is_set())

    async def test_conflict_on_another_method_does_not_stop_polling(self):
        guard = PollingConflictGuard()
        method = SendMessage(chat_id=1, text='Test')
        request = AsyncMock(side_effect=TelegramConflictError(method=method, message='conflict'))
        with self.assertRaises(TelegramConflictError):
            await guard(request, None, method)
        self.assertFalse(guard.conflict.is_set())

    async def test_manual_restart_clears_stale_conflict_after_clean_stop(self):
        bot = Bot('123456:unit-test-token')
        dispatcher = Dispatcher()
        requested = asyncio.Event()
        async def respond(bot, method, **kwargs):
            if isinstance(method, GetMe):
                return User(id=123456, is_bot=True, first_name='Test')
            requested.set()
            await asyncio.Event().wait()
        bot.session.make_request = AsyncMock(side_effect=respond)
        bot.session.close = AsyncMock()
        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory) / 'conflict.json'
            status.write_text('{}')
            task = asyncio.create_task(guarded_polling(dispatcher, bot, status))
            await asyncio.wait_for(requested.wait(), 3)
            await dispatcher.stop_polling()
            await asyncio.wait_for(task, 3)
            self.assertFalse(status.exists())
