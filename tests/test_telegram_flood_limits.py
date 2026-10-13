from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock

from aiogram.exceptions import TelegramRetryAfter
from telegram_client.notification_transport import send_notification
from telegram_client.screens import Screen


class TelegramFloodLimitTests(IsolatedAsyncioTestCase):
    async def test_429_returns_retry_delay_without_sending_again(self):
        bot = SimpleNamespace(send_rich_message=AsyncMock(side_effect=TelegramRetryAfter(
            method=Mock(), message='flood', retry_after=42)), send_message=AsyncMock())
        result = await send_notification(bot, 101, Screen('Title'))
        self.assertEqual(result, 'retry')
        self.assertEqual(result.retry_after, 42)
        self.assertEqual(bot.send_rich_message.await_count, 1)
        bot.send_message.assert_not_awaited()

    async def test_timeout_stays_unknown_without_retry(self):
        bot = SimpleNamespace(send_rich_message=AsyncMock(side_effect=TimeoutError()), send_message=AsyncMock())
        self.assertEqual(await send_notification(bot, 101, Screen('Title')), 'unknown')
        self.assertEqual(bot.send_rich_message.await_count, 1)
