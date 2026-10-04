"""Persisted account locale wins over Telegram defaults after adapter restart."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from telegram_client.backend import BackendError
from telegram_client.routers.common import LocaleMiddleware, NotificationStateMiddleware


class LocaleRestoreTests(IsolatedAsyncioTestCase):
    async def test_existing_callback_restores_saved_locale_in_both_directions(self):
        for saved, telegram in (('en', 'ru'), ('ru', 'en')):
            state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=10, user_id=10))
            backend = SimpleNamespace(me=AsyncMock(return_value={'locale': saved}))
            event = SimpleNamespace(callback_query=SimpleNamespace(
                from_user=SimpleNamespace(id=10, language_code=telegram)), message=None)
            handler = AsyncMock()
            middleware = LocaleMiddleware()
            data = {'state': state, 'backend': backend}
            await middleware(handler, event, data)
            self.assertEqual((await state.get_data())['locale'], saved)
            await middleware(handler, event, data)
            backend.me.assert_awaited_once_with(10)
            self.assertEqual(handler.await_count, 2)

    async def test_backend_failure_does_not_pin_fallback_language(self):
        state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=10, user_id=10))
        backend = SimpleNamespace(me=AsyncMock(side_effect=[BackendError('backend_unavailable', 503), {'locale': 'en'}]))
        event = SimpleNamespace(message=SimpleNamespace(from_user=SimpleNamespace(id=10, language_code='ru')), callback_query=None)
        middleware = LocaleMiddleware()
        data = {'state': state, 'backend': backend}
        await middleware(AsyncMock(), event, data)
        self.assertTrue((await state.get_data())['locale_restore_pending'])
        await middleware(AsyncMock(), event, data)
        self.assertEqual((await state.get_data())['locale'], 'en')
        self.assertFalse((await state.get_data())['locale_restore_pending'])

    async def test_notification_uses_restored_main_language(self):
        state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=10, user_id=10))
        backend = SimpleNamespace(me=AsyncMock(return_value={'locale': 'en'}))
        event = SimpleNamespace(message=None, callback_query=SimpleNamespace(
            data='notification_review:r1', message=SimpleNamespace(message_id=77),
            from_user=SimpleNamespace(id=10, language_code='ru')))
        result = {}
        async def handler(event, data):
            result.update(await data['state'].get_data())
        async def route(event, data):
            return await NotificationStateMiddleware()(handler, event, data)
        await LocaleMiddleware()(route, event, {'state': state, 'backend': backend})
        self.assertEqual(result['locale'], 'en')
        self.assertTrue(result['notification_session'])
