"""Device UI behavior, command identities and safe navigation."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.backend import BackendClient, BackendError
from telegram_client.i18n import CATALOG
from telegram_client.routers import user, user_devices
from tests import test_telegram_client


class TelegramDeviceTests(IsolatedAsyncioTestCase):
    setUp = test_telegram_client.TelegramFlowTests.setUp

    def backend(self, items):
        return SimpleNamespace(request=AsyncMock(return_value={'items': items}))

    async def test_single_device_skips_picker_forward_and_backward_without_reissuing(self):
        backend = self.backend([{'id': 'phone', 'display_name': 'My phone', 'status': 'active'}])
        with patch.object(user, 'issue', new_callable=AsyncMock) as issue, \
             patch.object(user, 'show_profile', new_callable=AsyncMock) as profile, \
             patch.object(user_devices, 'render', new_callable=AsyncMock) as draw:
            await user_devices.show_picker(123, 123, 77, 'profile', 'node', self.bot, backend, self.state)
            issue.assert_awaited_once()
            self.assertEqual(self.state_data['_navigation_devices']['phone'], 'My phone')
            issue.reset_mock()
            await user_devices.handle_action(user.Action(123, 'device_picker_back', ('profile', 'node')),
                self.query, self.bot, backend, self.state)
            profile.assert_awaited_once()
            issue.assert_not_awaited()
            draw.assert_not_awaited()

    async def test_awg_config_title_and_path_use_device_name_and_back_checks_picker(self):
        self.state_data.update(_navigation_nodes={'node': 'Petersburg #1'}, _navigation_devices={'phone': 'My phone'})
        backend = SimpleNamespace(issuance=AsyncMock(return_value={'status': 'succeeded',
            'profile_id': 'profile', 'node_key': 'node', 'protocol': 'awg', 'transport': 'vpn', 'device_id': 'phone'}),
            artifact=AsyncMock(return_value={'filename': 'config.vpn', 'content': 'vpn://config',
                'display_name': 'Petersburg #1 AmneziaWG · username · My phone'}))
        with patch.object(user, 'render', new_callable=AsyncMock, return_value=True) as draw:
            await user.show_issuance(123, 123, 77, 'issuance', self.bot, backend, self.state)
        screen, rows = draw.call_args.args[2:4]
        self.assertEqual(screen.title, 'My phone')
        actions = [user.actions[p.callback[2:]].name for p in screen.breadcrumbs]
        self.assertEqual(actions, ['home', 'profile', 'node', 'protocol', 'device_picker'])
        self.assertEqual(screen.breadcrumbs[2].label, 'Petersburg #1')
        self.assertEqual(user.actions[rows[-1][0].callback_data[2:]].name, 'device_picker_back')
        self.assertEqual(user.actions[screen.navigation_return[2:]].name, 'issuance')

    async def test_multi_device_picker_never_issues_without_selection(self):
        backend = self.backend([{'id': 'b', 'display_name': 'Laptop', 'status': 'active'},
            {'id': 'a', 'display_name': 'Phone', 'status': 'active'},
            {'id': 'c', 'display_name': 'Old', 'status': 'deleting'}])
        with patch.object(user_devices, 'render', new_callable=AsyncMock) as draw, \
             patch.object(user, 'issue', new_callable=AsyncMock) as issue:
            await user_devices.show_picker(123, 123, 77, 'profile', 'node', self.bot, backend, self.state)
        issue.assert_not_awaited()
        actions = [user.actions[b.callback_data[2:]] for row in draw.call_args.args[3] for b in row]
        selected = [action.args[-1] for action in actions if action.name == 'device_pick']
        self.assertEqual(selected, ['b', 'a'])
        self.assertTrue(draw.call_args.args[2].embedded_buttons)

    async def test_picker_paginates_and_empty_state_offers_creation(self):
        items = [{'id': str(i), 'display_name': f'Device {i:02}', 'status': 'active'} for i in range(11)]
        backend = self.backend(items)
        with patch.object(user_devices, 'render', new_callable=AsyncMock) as draw:
            await user_devices.show_picker(123, 123, 77, 'profile', 'node', self.bot, backend, self.state)
            actions = [user.actions[b.callback_data[2:]] for row in draw.call_args.args[3] for b in row]
            self.assertEqual(sum(a.name == 'device_pick' for a in actions), 10)
            self.assertTrue(any(a.name == 'device_picker_page' for a in actions))
            backend.request.return_value = {'items': []}
            await user_devices.show_picker(123, 123, 77, 'profile', 'node', self.bot, backend, self.state)
            self.assertTrue(draw.call_args.args[2].lines)
            actions = [user.actions[b.callback_data[2:]] for row in draw.call_args.args[3] for b in row]
            self.assertIn('device_create', [a.name for a in actions])

    async def test_pending_server_changes_offer_retry_instead_of_generic_error(self):
        with patch.object(user, 'issue', new_callable=AsyncMock,
                side_effect=BackendError('profile_not_synced', 409)), \
             patch.object(user_devices, 'render', new_callable=AsyncMock) as draw:
            await user_devices.select_device(123, 123, 77, 'profile', 'node', 'phone',
                self.bot, SimpleNamespace(), self.state)
        button = draw.call_args.args[3][0][0]
        self.assertEqual(user.actions[button.callback_data[2:]].args, ('profile', 'node', 'phone'))

    async def test_name_retry_preserves_command_identity_but_changed_input_gets_new_key(self):
        backend = SimpleNamespace(request=AsyncMock(side_effect=BackendError('backend_unavailable', 503)))
        self.state_data['device_name_draft'] = {'profile_id': 'profile', 'device_id': None,
            'revision': 4, 'owner': 123, 'message_id': 77}
        message = SimpleNamespace(text='My laptop', delete=AsyncMock(),
            from_user=SimpleNamespace(id=123), chat=SimpleNamespace(id=123, type='private'))
        with patch.object(user_devices, 'render', new_callable=AsyncMock):
            await user_devices.name_message(message, self.bot, backend, self.state)
            await user_devices.name_message(message, self.bot, backend, self.state)
            message.text = 'Phone'
            await user_devices.name_message(message, self.bot, backend, self.state)
        calls = backend.request.await_args_list
        self.assertEqual(calls[0].kwargs['command_key'], calls[1].kwargs['command_key'])
        self.assertNotEqual(calls[0].kwargs['command_key'], calls[2].kwargs['command_key'])
        self.assertEqual(calls[0].kwargs['revision'], 4)

    async def test_name_conflict_keeps_form_and_success_returns_device_card(self):
        backend = SimpleNamespace(request=AsyncMock(side_effect=[
            BackendError('device_name_conflict', 409), {'device': {'id': 'new'}}]))
        self.state_data['device_name_draft'] = {'profile_id': 'profile', 'device_id': None,
            'revision': 4, 'owner': 123, 'message_id': 77}
        await self.state.set_state(user_devices.DeviceName.waiting)
        message = SimpleNamespace(text='Laptop', delete=AsyncMock(),
            from_user=SimpleNamespace(id=123), chat=SimpleNamespace(id=123, type='private'))
        with patch.object(user_devices, 'render', new_callable=AsyncMock), \
             patch.object(user_devices, 'show_card', new_callable=AsyncMock) as card:
            await user_devices.name_message(message, self.bot, backend, self.state)
            self.assertEqual(await self.state.get_state(), user_devices.DeviceName.waiting.state)
            message.text = 'Phone'
            await user_devices.name_message(message, self.bot, backend, self.state)
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(card.call_args.args[3:5], ('profile', 'new'))

    async def test_creation_from_config_picker_returns_to_the_same_server(self):
        backend = SimpleNamespace(request=AsyncMock(side_effect=[
            {'desired_revision': 7}, {'device': {'id': 'new'}}]))
        with patch.object(user_devices, 'render', new_callable=AsyncMock) as draw:
            await user_devices.prompt_name(123, 123, 77, 'profile', None,
                self.bot, backend, self.state, node_key='node')
        back = user.actions[draw.call_args.args[3][0][0].callback_data[2:]]
        self.assertEqual((back.name, back.args), ('device_picker', ('profile', 'node')))
        message = SimpleNamespace(text='Phone', delete=AsyncMock(),
            from_user=SimpleNamespace(id=123), chat=SimpleNamespace(id=123, type='private'))
        with patch.object(user_devices, 'show_picker', new_callable=AsyncMock) as picker:
            await user_devices.name_message(message, self.bot, backend, self.state)
        self.assertEqual(picker.call_args.args[3:5], ('profile', 'node'))
        self.assertEqual(backend.request.call_args.kwargs['revision'], 7)

    async def test_delete_requires_live_confirmation_and_captures_revision(self):
        backend = self.backend([{'id': 'phone', 'display_name': 'Phone', 'status': 'active', 'revision': 3}])
        action = user.Action(123, 'device_delete', ('profile', 'phone'))
        with patch.object(user_devices, 'render', new_callable=AsyncMock) as draw:
            await user_devices.handle_action(action, self.query, self.bot, backend, self.state)
        confirm = draw.call_args.args[3][0][0]
        self.assertEqual(confirm.style, 'danger')
        captured = user.actions[confirm.callback_data[2:]]
        self.assertEqual(captured.args[2], '3')
        backend.request.reset_mock()
        with patch.object(user_devices, 'show_card', new_callable=AsyncMock):
            await user_devices.handle_action(captured, self.query, self.bot, backend, self.state)
            self.assertEqual(backend.request.call_args.args[0], 'DELETE')
            self.assertEqual(backend.request.call_args.kwargs['revision'], 3)
            backend.request.reset_mock()
            await user_devices.handle_action(captured, self.query, self.bot, backend, self.state)
            backend.request.assert_not_awaited()

    async def test_home_navigation_invalidates_delete_confirmation(self):
        self.state_data['device_delete_confirmation'] = {'key': 'old', 'message_id': 77}
        self.query.data = user.button(123, 'Back', 'home').callback_data
        with patch.object(user, 'show_home', new_callable=AsyncMock):
            await user.user_action_cb(self.query, self.bot, SimpleNamespace(), self.state)
        self.assertIsNone(self.state_data['device_delete_confirmation'])

    async def test_config_client_sends_explicit_device_only_when_selected(self):
        backend = BackendClient(SimpleNamespace(), 'http://127.0.0.1:8080', 'token')
        with patch.object(backend, 'request', new_callable=AsyncMock) as request:
            await backend.issue(123, 'profile', 'node', 'awg', 'vpn', device_id='phone')
            self.assertEqual(request.call_args.kwargs['body']['device_id'], 'phone')
            await backend.issue(123, 'profile', 'node', 'xray', 'tcp')
            self.assertNotIn('device_id', request.call_args.kwargs['body'])

    def test_all_device_texts_are_translated(self):
        en = {key for key in CATALOG['en'] if key.startswith('devices.')}
        ru = {key for key in CATALOG['ru'] if key.startswith('devices.')}
        self.assertEqual(en, ru)
        self.assertGreater(len(en), 15)
