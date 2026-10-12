import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, EditMessageText
from telegram_client.callback_answers import CallbackAnswerScope, ConcurrentCallbackAnswers, PENDING
from telegram_client.timings import ActionTimings, CURRENT, TelegramTimings


class CallbackAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def test_screen_is_sent_while_empty_acknowledgement_waits(self):
        started, release, screen = asyncio.Event(), asyncio.Event(), asyncio.Event()
        timing = TelegramTimings()
        async def request(bot, method):
            if isinstance(method, AnswerCallbackQuery):
                started.set()
                await release.wait()
            else:
                screen.set()
            return True
        async def next_request(bot, method):
            return await timing(request, bot, method)
        session = ConcurrentCallbackAnswers()
        async def handler(event, data):
            await session(next_request, None, AnswerCallbackQuery(callback_query_id='fixture'))
            await session(next_request, None, EditMessageText(text='fixture', chat_id=1, message_id=1))
            self.assertIsNotNone(CURRENT.get().screen_submitted_ms)
        async def scoped(event, data):
            return await CallbackAnswerScope()(handler, event, data)
        with patch('telegram_client.timings.enabled', return_value=True):
            task = asyncio.create_task(ActionTimings()(scoped, SimpleNamespace(callback_query=None), {}))
            await asyncio.wait_for(screen.wait(), 1)
            await asyncio.wait_for(started.wait(), 1)
            self.assertFalse(task.done())
            release.set()
            await task
        self.assertIsNone(PENDING.get())

    async def test_text_alert_and_url_answers_stay_synchronous(self):
        session = ConcurrentCallbackAnswers()
        called = []
        async def request(bot, method):
            called.append(method)
            return False
        async def handler(event, data):
            for fields in ({'text': 'notice'}, {'show_alert': True}, {'url': 'https://example.com'}):
                method = AnswerCallbackQuery(callback_query_id='fixture', **fields)
                self.assertFalse(await session(request, None, method))
                self.assertIs(called[-1], method)
            self.assertEqual(PENDING.get().tasks, [])
        await CallbackAnswerScope()(handler, None, {})

    async def test_expired_acknowledgement_does_not_discard_screen(self):
        session = ConcurrentCallbackAnswers()
        method = AnswerCallbackQuery(callback_query_id='fixture')
        async def request(bot, method):
            raise TelegramBadRequest(method, 'expired')
        async def handler(event, data):
            await session(request, None, method)
            return 'screen delivered'
        with self.assertLogs('telegram_client.callback_answers', 'WARNING'):
            self.assertEqual(await CallbackAnswerScope()(handler, None, {}), 'screen delivered')

    async def test_cancelling_handler_cancels_pending_acknowledgement(self):
        started = asyncio.Event()
        stopped = asyncio.Event()
        async def request(bot, method):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        async def handler(event, data):
            await ConcurrentCallbackAnswers()(request, None, AnswerCallbackQuery(callback_query_id='fixture'))
            await asyncio.Event().wait()
        task = asyncio.create_task(CallbackAnswerScope()(handler, None, {}))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stopped.is_set())
        self.assertIsNone(PENDING.get())
