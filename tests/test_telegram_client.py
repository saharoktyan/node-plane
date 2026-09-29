import asyncio
from unittest.mock import patch
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from pathlib import Path
from aiogram.exceptions import TelegramBadRequest

from telegram_client.main import TelegramClient
from telegram_client.backend import BackendError
from telegram_client.screens import Screen


class TelegramClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = SimpleNamespace(
            send_rich_message=AsyncMock(return_value=SimpleNamespace(message_id=10)),
            edit_message_text=AsyncMock(), send_document=AsyncMock(),
            send_photo=AsyncMock(),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=11)))
        self.backend = SimpleNamespace(
            me=AsyncMock(return_value={'status': 'approved', 'role': 'member'}),
            profiles=AsyncMock(return_value={'items': [
                {'id': 'abc', 'display_name': 'My VPN'}]}),
            profile_nodes=AsyncMock(return_value={'items': [
                {'key': 'lv1', 'title': 'Latvia #1', 'region': 'EU', 'flag': '🇱🇻',
                 'protocols': [{'kind': 'xray', 'transports': ['tcp', 'xhttp']},
                               {'kind': 'awg', 'transports': ['vpn', 'conf']}]}]}),
            request=AsyncMock(return_value={'display_name': 'My VPN', 'frozen': False}),
            accounts=AsyncMock(return_value={'items': []}),
            admin_profiles=AsyncMock(return_value={'items': []}),
            admin_nodes=AsyncMock(return_value={'items': []}),
            create_node=AsyncMock(return_value={'key': 'lv1'}),
            edit_node=AsyncMock(return_value={'key': 'lv1'}),
            rollout_agent=AsyncMock(return_value={'id': 'rollout-1'}),
            agent_rollout=AsyncMock(return_value={'id': 'rollout-1',
                'node_key': 'lv1', 'status': 'awaiting_executor'}),
            node_maintenance=AsyncMock(return_value={'node_key': 'lv1',
                'status': 'active', 'pending_tasks': 0, 'blocked_tasks': 0,
                'cleanup_phase': None}),
            retire_node_registry_only=AsyncMock(return_value={'node_key': 'lv1'}),
            node_runtime=AsyncMock(return_value={'health_state': 'running',
                'xray_config_present': True, 'awg_config_present': True}),
            apply_node_settings=AsyncMock(return_value={'id': 'node-operation'}),
            node_settings_operation=AsyncMock(return_value={'status': 'succeeded'}),
            profile_grants=AsyncMock(return_value={'items': [
                {'node_key': 'lv1', 'protocol': 'awg'}]}),
            replace_grants=AsyncMock(),
            create_profile=AsyncMock(return_value={'profile': {'id': 'created'}}),
            issue=AsyncMock(return_value={'id': 'issued'}),
            issuance=AsyncMock(return_value={'status': 'succeeded'}),
            artifact=AsyncMock(return_value={'filename': 'awg-lv1.vpn',
                                             'content': 'vpn://encoded'}))
        self.client = TelegramClient(self.bot, self.backend)

    async def test_rich_screen_edits_one_control_message(self):
        await self.client.home(100, 200)
        sent = self.bot.send_rich_message.call_args.kwargs
        self.assertEqual(sent['rich_message'].blocks[0].type.value, 'heading')
        self.assertEqual(sent['rich_message'].blocks[1].type.value, 'paragraph')
        self.assertLess(len(sent['reply_markup'].inline_keyboard[0][0].callback_data), 64)
        await self.client.profiles(100, 200, 10)
        edited = self.bot.edit_message_text.call_args.kwargs
        self.assertEqual(edited['message_id'], 10)
        self.assertIsNone(edited.get('text'))

    async def test_awg_issuance_attaches_file_after_backend_confirmation(self):
        await self.client.issue(100, 200, 10, 'profile', 'lv1', 'awg', 'vpn')
        document = self.bot.send_document.call_args.args[1]
        self.assertEqual(document.filename, 'awg-lv1.vpn')
        self.assertEqual(document.data, b'vpn://encoded')
        self.backend.artifact.assert_awaited_once_with(200, 'issued')

    async def test_foreign_user_cannot_consume_callback_token(self):
        button = self.client.button(200, 'Home', 'home')
        query = SimpleNamespace(data=button.callback_data,
            from_user=SimpleNamespace(id=300),
            message=SimpleNamespace(chat=SimpleNamespace(id=100), message_id=10),
            answer=AsyncMock())
        await self.client.callback(query)
        query.answer.assert_awaited_once()
        self.assertIn(button.callback_data[3:], self.client.actions)

    def test_details_are_rich_blocks(self):
        blocks = Screen('Config', ('Ready',), 'Details', ('Hidden',)).rich().blocks
        self.assertEqual(blocks[-1].type.value, 'details')
        self.assertEqual(blocks[-1].blocks[0].text, 'Hidden')

    async def test_add_grant_preserves_existing_grants_and_revision(self):
        self.backend.request.return_value = {'desired_revision': 7}
        await self.client.change_grant(200, 'profile', 'lv1', 'xray', add=True)
        self.backend.replace_grants.assert_awaited_once_with(200, 'profile', 7,
            [{'node_key': 'lv1', 'protocol': 'awg'},
             {'node_key': 'lv1', 'protocol': 'xray'}])

    async def test_create_profile_from_message_uses_pending_account(self):
        await self.client.home(100, 200)
        from telegram_client.main import Draft
        self.client.drafts[200] = Draft('account-id', 10, float('inf'), 'command-id')
        message = SimpleNamespace(from_user=SimpleNamespace(id=200),
            chat=SimpleNamespace(id=100, type='private'), text='New user')
        await self.client.text_input(message)
        self.backend.create_profile.assert_awaited_once_with(200, 'account-id',
            'New user', 'command-id')
        self.assertNotIn(200, self.client.drafts)

    async def test_node_apply_waits_for_confirmed_backend_result(self):
        self.backend.request.return_value = {'key': 'lv1', 'title': 'Latvia',
            'enabled': False, 'protocols': ['awg'], 'desired_revision': 2,
            'applied_revision': 1}
        await self.client.apply_node(100, 200, 10, 'lv1')
        self.backend.apply_node_settings.assert_awaited_once_with(200, 'lv1', 2)
        self.backend.node_settings_operation.assert_awaited_once_with(200, 'node-operation')

    async def test_profile_create_retries_with_same_command_identity(self):
        from telegram_client.main import Draft
        self.client.drafts[200] = Draft('account-id', 10, float('inf'), 'same-key')
        self.backend.create_profile.side_effect = [BackendError('backend_unavailable', 503),
            {'profile': {'id': 'created'}}]
        message = SimpleNamespace(from_user=SimpleNamespace(id=200),
            chat=SimpleNamespace(id=100, type='private'), text='New user')
        await self.client.text_input(message)
        self.assertEqual(self.client.drafts[200].command_key, 'same-key')
        await self.client.text_input(message)
        self.assertEqual([call.args[-1] for call in self.backend.create_profile.await_args_list],
                         ['same-key', 'same-key'])

    async def test_node_registration_submits_defaults_and_opens_node_card(self):
        from telegram_client.main import NodeDraft
        self.client.node_drafts[200] = NodeDraft(10, 'node-key', float('inf'))
        self.backend.request.return_value = {'key': 'lv1', 'title': 'Latvia #1',
            'enabled': False, 'protocols': ['awg', 'xray'],
            'desired_revision': 1, 'applied_revision': 0}
        message = SimpleNamespace(from_user=SimpleNamespace(id=200),
            chat=SimpleNamespace(id=100, type='private'),
            text='lv1 | Latvia #1 | EU | lv1.example.com')
        await self.client.text_input(message)
        self.assertEqual(self.client.node_drafts[200].base['public_host'], 'lv1.example.com')
        await self.client.submit_node(100, 200, 10, 'both')
        body = self.backend.create_node.await_args.args[1]
        self.assertEqual(body['protocols'], ['awg', 'xray'])
        self.assertEqual(body['settings']['xray_xhttp_port'], 8443)
        self.assertEqual(self.backend.create_node.await_args.args[2], 'node-key')
        self.assertNotIn(200, self.client.node_drafts)
        self.assertEqual(self.bot.edit_message_text.call_args.kwargs['message_id'], 10)

    async def test_node_registration_reuses_command_key_after_lost_response(self):
        from telegram_client.main import NodeDraft
        self.client.node_drafts[200] = NodeDraft(10, 'same-key', float('inf'),
            {'key': 'lv1', 'title': 'Latvia', 'region': 'EU',
             'public_host': 'lv1.example.com'})
        self.backend.request.return_value = {'key': 'lv1', 'title': 'Latvia',
            'enabled': False, 'protocols': ['awg'],
            'desired_revision': 1, 'applied_revision': 0}
        self.backend.create_node.side_effect = [BackendError('backend_unavailable', 503),
            {'key': 'lv1'}]
        await self.client.submit_node(100, 200, 10, 'awg')
        await self.client.submit_node(100, 200, 10, 'awg')
        self.assertEqual([call.args[2] for call in self.backend.create_node.await_args_list],
                         ['same-key', 'same-key'])

    async def test_ssh_rollout_accepts_target_only_and_preserves_retry_key(self):
        from telegram_client.main import AgentDraft
        self.client.agent_drafts[200] = AgentDraft('lv1', 10, 'rollout-key', float('inf'))
        self.backend.rollout_agent.side_effect = [BackendError('backend_unavailable', 503),
            {'id': 'rollout-1'}]
        message = SimpleNamespace(from_user=SimpleNamespace(id=200),
            chat=SimpleNamespace(id=100, type='private'), text='root@lv1.example.com')
        await self.client.text_input(message)
        self.assertEqual(self.client.agent_drafts[200].ssh_target, 'root@lv1.example.com')
        await self.client.text_input(message)
        self.assertNotIn(200, self.client.agent_drafts)
        self.assertEqual([call.kwargs['command_key'] for call in
            self.backend.rollout_agent.await_args_list], ['rollout-key', 'rollout-key'])
        self.assertEqual(self.backend.rollout_agent.await_args.kwargs['ssh_target'],
                         'root@lv1.example.com')

    async def test_rollout_success_message_is_confirmed_status(self):
        self.backend.agent_rollout.return_value = {'id': 'rollout-1',
            'node_key': 'lv1', 'status': 'succeeded'}
        await self.client.rollout_status(100, 200, 10, 'rollout-1')
        rich = self.bot.edit_message_text.call_args.kwargs['rich_message']
        self.assertIn('installed', rich.blocks[1].text)

    async def test_qr_is_generated_from_fresh_authorized_artifact(self):
        self.backend.artifact.return_value = {'filename': 'test.txt',
                                              'content': 'vless://example'}
        await self.client.show_qr(100, 200, 10, 'issued', 'profile', 'lv1')
        self.backend.artifact.assert_awaited_once_with(200, 'issued')
        photo = self.bot.send_photo.call_args.args[1]
        self.assertTrue(photo.data.startswith(b'\x89PNG'))

    async def test_rich_message_failure_falls_back_to_plain_text(self):
        failure = TelegramBadRequest(method=None, message='rich messages unsupported')
        self.bot.send_rich_message.side_effect = failure
        await self.client.home(100, 200)
        self.assertEqual(self.bot.send_message.await_args.kwargs['text'],
                         'Node Plane\n\nChoose what to manage.')

    async def test_refresh_runtime_creates_new_revision_then_applies_it(self):
        node = {'key': 'lv1', 'title': 'Latvia',
            'enabled': True, 'protocols': ['awg'], 'desired_revision': 2,
            'applied_revision': 1, 'settings': {'awg_port': 51820}}
        self.backend.request.side_effect = [node, {**node, 'desired_revision': 3},
                                            {**node, 'desired_revision': 3}]
        self.backend.edit_node.return_value = {'key': 'lv1', 'desired_revision': 3}
        await self.client.refresh_runtime(100, 200, 10, 'lv1')
        self.backend.edit_node.assert_awaited_once()
        self.assertEqual(self.backend.edit_node.await_args.args[:4],
                         (200, 'lv1', 2, {'settings': {'awg_port': 51820}}))
        self.assertEqual(self.backend.apply_node_settings.await_args.args[:3],
                         (200, 'lv1', 3))
        self.assertNotIn(200, self.client.runtime_refresh_drafts)

    async def test_pending_tasks_stay_pending_and_keep_refresh_action(self):
        self.backend.issuance.return_value = {'status': 'awaiting_executor'}
        await self.client.issuance_status(100, 200, 10, 'issued',
                                          'profile', 'lv1', 'awg', 'vpn')
        screen = self.bot.edit_message_text.call_args.kwargs['rich_message']
        self.assertIn('Still preparing', screen.blocks[0].text)
        self.backend.node_settings_operation.return_value = {'status': 'running'}
        await self.client.node_apply_status(100, 200, 10, 'task', 'lv1')
        screen = self.bot.edit_message_text.call_args.kwargs['rich_message']
        self.assertIn('Still applying', screen.blocks[0].text)

    async def test_update_button_hidden_when_runtime_matches_release(self):
        version = (Path(__file__).resolve().parents[1] / 'VERSION').read_text().strip()
        self.backend.node_runtime.return_value = {'runtime_version': version}
        await self.client.node_updates(100, 200, 10, 'lv1')
        buttons = [button.text for row in
            self.bot.edit_message_text.call_args.kwargs['reply_markup'].inline_keyboard
            for button in row]
        self.assertNotIn('Refresh runtime', buttons)

    async def test_local_rollout_retries_with_same_command_key(self):
        from telegram_client.main import AgentDraft
        self.client.agent_drafts[200] = AgentDraft('lv1', 10, 'local-key', float('inf'))
        self.backend.rollout_agent.side_effect = [BackendError('backend_unavailable', 503),
            {'id': 'rollout-1'}]
        await self.client.retry_rollout(100, 200, 10)
        self.assertIn(200, self.client.agent_drafts)
        await self.client.retry_rollout(100, 200, 10)
        self.assertEqual([call.kwargs['command_key'] for call in
            self.backend.rollout_agent.await_args_list], ['local-key', 'local-key'])
        self.assertNotIn(200, self.client.agent_drafts)

    async def test_node_setting_edit_saves_desired_state_only(self):
        self.backend.request.return_value = {'key': 'lv1', 'title': 'Latvia',
            'region': 'EU', 'flag': '', 'protocols': ['awg', 'xray'],
            'desired_revision': 4, 'applied_revision': 3,
            'settings': {'public_host': 'old.example', 'awg_port': 51820}}
        await self.client.edit_node_field(100, 200, 10, 'lv1', 'public_host')
        draft = self.client.node_edit_drafts[200]
        message = SimpleNamespace(from_user=SimpleNamespace(id=200),
            chat=SimpleNamespace(id=100, type='private'), text='new.example')
        await self.client.text_input(message)
        self.backend.edit_node.assert_awaited_once_with(200, 'lv1', 4,
            {'settings': {'public_host': 'new.example', 'awg_port': 51820}},
            draft.command_key)
        self.backend.apply_node_settings.assert_not_awaited()

    async def test_node_setting_retry_keeps_payload_and_key(self):
        from telegram_client.main import NodeEditDraft
        self.client.node_edit_drafts[200] = NodeEditDraft('lv1', 'region', 10, 2,
            {}, 'same-key', float('inf'))
        self.backend.edit_node.side_effect = [BackendError('backend_unavailable', 503),
            {'key': 'lv1'}]
        self.backend.request.return_value = {'key': 'lv1', 'title': 'Latvia',
            'region': 'North', 'flag': '', 'protocols': ['awg'],
            'desired_revision': 3, 'applied_revision': 2, 'settings': {}}
        message = SimpleNamespace(from_user=SimpleNamespace(id=200),
            chat=SimpleNamespace(id=100, type='private'), text='North')
        await self.client.text_input(message)
        await self.client.text_input(message)
        self.assertEqual([call.args[-1] for call in self.backend.edit_node.await_args_list],
                         ['same-key', 'same-key'])

    async def test_registry_removal_requires_confirmation_screen(self):
        await self.client.confirm_registry_removal(100, 200, 10, 'lv1')
        self.backend.retire_node_registry_only.assert_not_awaited()
        await self.client.retire_registry(100, 200, 10, 'lv1')
        self.backend.retire_node_registry_only.assert_awaited_once_with(200, 'lv1')

    async def test_request_notifies_only_approved_backend_admin(self):
        self.backend.request_access = AsyncMock(return_value={'id': 'request-1'})
        self.backend.me.side_effect = [
            {'status': 'pending', 'role': 'member'},
            {'status': 'approved', 'role': 'admin'},
            {'status': 'approved', 'role': 'member'}]
        self.backend.request.return_value = {'items': [{'status': 'pending'}]}
        button = self.client.button(200, 'Request', 'request_access')
        query = SimpleNamespace(data=button.callback_data,
            from_user=SimpleNamespace(id=200),
            message=SimpleNamespace(chat=SimpleNamespace(id=100), message_id=10),
            answer=AsyncMock())
        with patch.dict('os.environ', {'ADMIN_IDS': '101,102'}):
            await self.client.callback(query)
        self.assertEqual(self.bot.send_rich_message.await_count, 1)
        self.assertEqual(self.bot.send_rich_message.await_args.kwargs['chat_id'], 101)


if __name__ == '__main__':
    unittest.main()
