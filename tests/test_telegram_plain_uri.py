"""Regular-message clipboard fallback must preserve authorization and the main panel."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey

from telegram_client.backend import BackendError
from telegram_client.routers import user


class PlainUriTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.storage = MemoryStorage()
        self.state = FSMContext(self.storage, StorageKey(bot_id=1, chat_id=123, user_id=123))
        await self.state.update_data(locale='en', control_message_id=77, artifact_message_ids=[99])
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=88)),
            delete_message=AsyncMock())
        self.query = SimpleNamespace(data='', answer=AsyncMock(), from_user=SimpleNamespace(id=123),
            message=SimpleNamespace(message_id=77, chat=SimpleNamespace(id=123, type='private')))
        self.backend = SimpleNamespace(
            issuance=AsyncMock(return_value={'status': 'succeeded', 'protocol': 'awg', 'transport': 'vpn'}),
            artifact=AsyncMock(return_value={'content': 'vpn://fresh-config'}))

    async def asyncTearDown(self):
        await self.storage.close()

    async def test_plain_delivery_for_awg_and_vless_keeps_panel_files_and_code_entities(self):
        for locale in ('ru', 'en'):
            await self.state.update_data(locale=locale)
            for protocol, transport, uri in (('awg', 'vpn', 'vpn://fresh-config'),
                ('xray', 'tcp', 'vless://test#🙂'), ('xray', 'xhttp', 'vless://xhttp')):
                self.backend.issuance.return_value = {'status': 'succeeded', 'protocol': protocol, 'transport': transport}
                self.backend.artifact.return_value = {'content': uri}
                self.query.data = user.button(123, 'Send link', 'issuance_plain', 'issuance1').callback_data
                with patch.object(user, 'render', new_callable=AsyncMock) as draw:
                    await user.user_action_cb(self.query, self.bot, self.backend, self.state)
                draw.assert_not_awaited()
                kwargs = self.bot.send_message.call_args.kwargs
                self.assertEqual(kwargs['text'], uri)
                self.assertIsNone(kwargs['parse_mode'])
                self.assertNotIn('rich_message', kwargs)
                self.assertTrue(kwargs['link_preview_options'].is_disabled)
                self.assertEqual((kwargs['entities'][0].type, kwargs['entities'][0].length),
                    ('code', len(uri.encode('utf-16-le')) // 2))
                self.assertEqual(kwargs['reply_markup'].inline_keyboard[0][0].callback_data, 'config_uri_close')
                data = await self.state.get_data()
                self.assertEqual(data['control_message_id'], 77)
                self.assertEqual(data['artifact_message_ids'], [99, 88])
        self.assertEqual(self.backend.artifact.await_count, 6)

    async def test_revoked_artifact_or_unfinished_issuance_cannot_send_cached_uri(self):
        self.backend.artifact.side_effect = BackendError('grant_revoked', 403)
        with self.assertRaises(BackendError):
            await user.send_plain_uri(123, 123, 'issuance1', self.bot, self.backend, self.state)
        self.backend.artifact.assert_awaited_once_with(123, 'issuance1')
        self.bot.send_message.assert_not_awaited()
        self.backend.artifact.reset_mock()
        self.backend.issuance.return_value['status'] = 'superseded'
        with self.assertRaises(BackendError):
            await user.send_plain_uri(123, 123, 'issuance1', self.bot, self.backend, self.state)
        self.backend.artifact.assert_not_awaited()

    async def test_navigation_during_artifact_read_prevents_late_delivery(self):
        async def navigate(*args):
            await self.state.update_data(issuance_poll_token=None)
            return {'content': 'vpn://fresh-config'}
        self.backend.artifact.side_effect = navigate
        await user.send_plain_uri(123, 123, 'issuance1', self.bot, self.backend, self.state)
        self.bot.send_message.assert_not_awaited()

    async def test_navigation_during_telegram_send_removes_the_late_auxiliary_message(self):
        async def navigate(**kwargs):
            await self.state.update_data(issuance_poll_token=None)
            return SimpleNamespace(message_id=88)
        self.bot.send_message.side_effect = navigate
        await user.send_plain_uri(123, 123, 'issuance1', self.bot, self.backend, self.state)
        self.bot.delete_message.assert_awaited_once_with(123, 88)
        self.assertEqual((await self.state.get_data())['artifact_message_ids'], [99])

    async def test_close_only_deletes_auxiliary_message_and_preserves_panel(self):
        await user.send_plain_uri(123, 123, 'issuance1', self.bot, self.backend, self.state)
        self.query.message.message_id = 88
        await user.close_plain_uri_cb(self.query, self.bot, self.state)
        self.bot.delete_message.assert_awaited_once_with(123, 88)
        data = await self.state.get_data()
        self.assertEqual(data['control_message_id'], 77)
        self.assertEqual(data['artifact_message_ids'], [99])
        self.query.message.message_id = 77
        await user.close_plain_uri_cb(self.query, self.bot, self.state)
        self.assertEqual(self.bot.delete_message.await_count, 1)

    async def test_repeated_plain_delivery_replaces_previous_uri_and_navigation_cleans_it(self):
        await user.send_plain_uri(123, 123, 'issuance1', self.bot, self.backend, self.state)
        self.bot.send_message.return_value.message_id = 89
        await user.send_plain_uri(123, 123, 'issuance1', self.bot, self.backend, self.state)
        self.bot.delete_message.assert_awaited_once_with(123, 88)
        self.bot.delete_message.reset_mock()
        await user.clear_artifacts(self.bot, 123, self.state)
        self.bot.delete_message.assert_any_await(123, 89)
        self.bot.delete_message.assert_any_await(123, 99)
        self.assertEqual((await self.state.get_data())['control_message_id'], 77)
