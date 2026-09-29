"""Exercise the Telegram paths that connect a person, node, and config."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from aiogram import Dispatcher

from telegram_client import main
from telegram_client.routers import (user, admin_requests, admin_profiles,
                                     admin_nodes, admin_settings)


class RouterStartupTests(TestCase):
    def test_all_routers_import_and_register(self):
        dispatcher = Dispatcher()
        for module in (user, admin_requests, admin_profiles, admin_nodes,
                       admin_settings):
            dispatcher.include_router(module.router)
        self.assertEqual(len(dispatcher.sub_routers), 5)
        self.assertTrue(callable(main.main))
        self.assertIn(admin_nodes.admin_node_cb,
            [handler.callback for handler in admin_nodes.router.callback_query.handlers])


class TelegramFlowTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = SimpleNamespace(send_document=AsyncMock(), send_photo=AsyncMock())
        self.state = SimpleNamespace(get_data=AsyncMock(return_value={}),
            update_data=AsyncMock(), clear=AsyncMock())
        self.query = SimpleNamespace(answer=AsyncMock(),
            from_user=SimpleNamespace(id=123),
            message=SimpleNamespace(chat=SimpleNamespace(id=123, type='private'),
                                    message_id=77))

    async def test_member_profile_comes_from_own_account(self):
        backend = SimpleNamespace(profiles=AsyncMock(return_value={'items': [
            {'id': 'p1', 'display_name': 'Personal'}]}),
            profile_nodes=AsyncMock(return_value={'items': [
                {'key': 'lv1', 'title': 'Latvia', 'region': 'EU', 'flag': '🇱🇻',
                 'protocols': [{'kind': 'xray', 'transports': ['tcp', 'xhttp']}]}]}),
            request=AsyncMock(return_value={'display_name': 'Personal', 'frozen': False}))
        with patch.object(user, 'render', new_callable=AsyncMock) as render:
            await user.show_profiles(123, 123, 77, self.bot, backend, self.state)
            backend.profiles.assert_awaited_once_with(123)
            await user.show_profile(123, 123, 77, 'p1', self.bot, backend, self.state)
            backend.profile_nodes.assert_awaited_once_with(123, 'p1')
            await user.show_node(123, 123, 77, 'p1', 'lv1', self.bot, backend, self.state)
            self.assertEqual(len(render.call_args.args[3]), 3)

    async def test_successful_issuance_sends_the_actual_config_file(self):
        backend = SimpleNamespace(
            issuance=AsyncMock(return_value={'status': 'succeeded',
                'profile_id': 'p1', 'node_key': 'lv1', 'protocol': 'awg'}),
            artifact=AsyncMock(return_value={'filename': 'Latvia.vpn',
                'content': 'vpn://fresh-config'}))
        with patch.object(user, 'render', new_callable=AsyncMock):
            await user.show_issuance(123, 123, 77, 'issuance1',
                                     self.bot, backend, self.state)
        attachment = self.bot.send_document.call_args.args[1]
        self.assertEqual(attachment.filename, 'Latvia.vpn')
        self.assertEqual(attachment.data, b'vpn://fresh-config')

    async def test_grants_use_backend_protocol_rows_and_profile_revision(self):
        backend = SimpleNamespace(
            profile_grants=AsyncMock(return_value={'items': [
                {'node_key': 'lv1', 'protocol': 'awg'}]}),
            request=AsyncMock(return_value={'desired_revision': 7}),
            replace_grants=AsyncMock())
        with patch.object(admin_profiles, 'show_grant_protocols', new_callable=AsyncMock):
            await admin_profiles.change_grant(self.query, 'p1', 'lv1', 'xray',
                True, self.bot, backend, self.state)
        backend.replace_grants.assert_awaited_once_with(123, 'p1', 7,
            [{'node_key': 'lv1', 'protocol': 'awg'},
             {'node_key': 'lv1', 'protocol': 'xray'}])

    async def test_protocol_buttons_pack_required_callback_fields(self):
        backend = SimpleNamespace(
            profile_grants=AsyncMock(return_value={'items': [
                {'node_key': 'lv1', 'protocol': 'awg'}]}),
            request=AsyncMock(return_value={'title': 'Latvia',
                'protocols': ['awg', 'xray']}))
        with patch.object(admin_profiles, 'render', new_callable=AsyncMock) as render:
            await admin_profiles.show_grant_protocols(123, 123, 77,
                '00000000-0000-0000-0000-000000000001', 'lv1',
                self.bot, backend, self.state)
        rows = render.call_args.args[3]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(len(row[0].callback_data.encode()) <= 64 for row in rows))

    async def test_node_wizard_queues_agent_through_backend(self):
        self.query.data = 'wizard_proto:done'
        self.state.get_data.return_value = {
            'create_node_command_key': 'create-key',
            'rollout_command_key': 'rollout-key',
            'wizard_data': {'key': 'lv1', 'title': 'Latvia', 'region': 'EU',
                            'flag': '🇱🇻', 'public_host': 'lv1.example.com',
                            'protocols': ['awg', 'xray'], 'transport': 'ssh',
                            'ssh_target': 'root@lv1.example.com'}}
        backend = SimpleNamespace(create_node=AsyncMock(return_value={'key': 'lv1'}),
            rollout_agent=AsyncMock(return_value={'id': 'rollout-id'}))
        with patch.object(admin_nodes, 'show_rollout_status', new_callable=AsyncMock):
            await admin_nodes.wizard_proto_cb(self.query, self.bot, backend, self.state)
        args, kwargs = backend.create_node.call_args
        self.assertEqual(args[0], 123)
        self.assertEqual(kwargs['command_key'], 'create-key')
        self.assertEqual(args[1]['xray_transports'], ['tcp', 'xhttp'])
        backend.rollout_agent.assert_awaited_once_with(123, 'lv1', 'ssh',
            ssh_target='root@lv1.example.com', command_key='rollout-key')
