"""Decision receipts remain closable and never replace a profile editor."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation

from telegram_client.access_decision_delivery import deliver_one


class AccessDecisionDeliveryTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = SimpleNamespace(id=1, edit_message_rich_message=AsyncMock(),
            edit_message_text=AsyncMock())
        self.delivery = dict(request_id='request', chat_id=123, message_id=99,
            locale='en', decision='approved', surface='workstation',
            decider={'id': 'actor', 'username': 'saharoktyan'})
        self.backend = SimpleNamespace(request=AsyncMock(side_effect=[
            {'delivery': self.delivery}, {'ok': True}]))
        self.storage = MemoryStorage()
        self.dispatcher = SimpleNamespace(storage=self.storage,
            fsm=SimpleNamespace(events_isolation=SimpleEventIsolation()))

    async def test_other_admin_receipt_has_close_and_workstation_attribution(self):
        self.assertTrue(await deliver_one(self.bot, self.backend, self.dispatcher))
        options = self.bot.edit_message_rich_message.call_args.kwargs
        self.assertEqual(options['message_id'], 99)
        content = options['rich_message'].model_dump_json()
        self.assertIn('notification_close', content)
        self.assertIn('Close', content)
        self.assertIn('@saharoktyan', content)
        self.assertIn('Workstation', content)
        self.assertNotIn('notification_decide', content)
        self.assertIn('/ack?', self.backend.request.call_args.args[1])

    async def test_completed_notice_workflow_is_not_overwritten(self):
        state = FSMContext(self.storage, StorageKey(bot_id=1, chat_id=123,
            user_id=123, destiny='notification:99'))
        await state.update_data(notification_result={'decision': 'approve'},
            profile_setup={'profile_id': 'p1'})
        self.assertTrue(await deliver_one(self.bot, self.backend, self.dispatcher))
        self.bot.edit_message_rich_message.assert_not_awaited()
        self.assertEqual((await state.get_data())['profile_setup'], {'profile_id': 'p1'})

    async def test_closed_notice_is_not_recreated(self):
        state = FSMContext(self.storage, StorageKey(bot_id=1, chat_id=123,
            user_id=123, destiny='notification:99'))
        await state.update_data(notification_closed=True)
        self.assertTrue(await deliver_one(self.bot, self.backend, self.dispatcher))
        self.bot.edit_message_rich_message.assert_not_awaited()

    async def test_deleted_notice_is_acknowledged_instead_of_blocking_other_admins(self):
        from aiogram.exceptions import TelegramNotFound
        from aiogram.methods import EditMessageText
        self.bot.edit_message_rich_message.side_effect = TelegramNotFound(
            method=EditMessageText(chat_id=123, message_id=99, text='receipt'),
            message='Not Found')
        self.assertTrue(await deliver_one(self.bot, self.backend, self.dispatcher))
        self.assertIn('/ack?', self.backend.request.call_args.args[1])

    async def test_rejection_receipt_has_close_and_attribution(self):
        self.delivery['decision'] = 'rejected'
        self.delivery['surface'] = 'telegram'
        self.delivery['decider'] = {'id': 'admin-1', 'username': 'reviewer'}
        self.assertTrue(await deliver_one(self.bot, self.backend, self.dispatcher))
        options = self.bot.edit_message_rich_message.call_args.kwargs
        self.assertEqual(options['message_id'], 99)
        content = options['rich_message'].model_dump_json()
        self.assertIn('notification_close', content)
        self.assertIn('@reviewer', content)
        self.assertNotIn('Workstation', content)
        self.assertIn('/ack?', self.backend.request.call_args.args[1])
