"""Exercise the Telegram paths that connect a person, node, and config."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch
from uuid import UUID

from aiogram import Dispatcher

from telegram_client import main
from telegram_client.backend import BackendError
from telegram_client.routers import (user, admin_requests, admin_profiles,
                                     admin_nodes, admin_settings)
from telegram_client.routers.callbacks import ProbeNodeCallback
from telegram_client.routers.callbacks import NewProfileCallback


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
        self.state_data = {}
        self.current_state = None
        async def get_data():
            return dict(self.state_data)
        async def update_data(**values):
            self.state_data.update(values)
        async def clear():
            self.state_data.clear()
        async def set_state(value):
            self.current_state = value.state if hasattr(value, 'state') else value
        async def get_state():
            return self.current_state
        self.state = SimpleNamespace(get_data=get_data, update_data=update_data,
            clear=clear, set_state=set_state, get_state=get_state)
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
            member_profile_summary=AsyncMock(return_value={
                'display_name': 'Personal', 'frozen': False, 'expired': False}))
        with patch.object(user, 'render', new_callable=AsyncMock) as render:
            await user.show_profiles(123, 123, 77, self.bot, backend, self.state)
            backend.profiles.assert_awaited_once_with(123)
            backend.profile_nodes.assert_awaited_once_with(123, 'p1')
            await user.show_profile(123, 123, 77, 'p1', self.bot, backend, self.state)
            self.assertEqual(backend.profile_nodes.await_count, 2)
            await user.show_node(123, 123, 77, 'p1', 'lv1', self.bot, backend, self.state)
            self.assertEqual(backend.profile_nodes.await_count, 3)
            self.assertEqual(len(render.call_args.args[3]), 2)

    async def test_member_account_shows_profile_access_and_statistics(self):
        summary = {'profile_id': 'p1', 'display_name': 'Personal',
            'frozen': False, 'expired': False, 'expires_at': None,
            'created_at': '2026-09-29T09:00:00+00:00',
            'nodes': [{'key': 'lv1', 'title': 'Latvia', 'flag': '🇱🇻',
                       'protocols': ['awg', 'xray']}],
            'node_count': 1, 'protocol_count': 2, 'awg_count': 1,
            'xray_count': 1, 'issued_count': 3,
            'last_issued_at': '2026-09-29T10:00:00+00:00'}
        backend = SimpleNamespace(
            me=AsyncMock(return_value={'id': 'account1', 'status': 'approved'}),
            profiles=AsyncMock(return_value={'items': [
                {'id': 'p1', 'display_name': 'Personal'}]}),
            member_profile_summary=AsyncMock(return_value=summary))
        with patch.object(user, 'render', new_callable=AsyncMock) as render:
            await user.show_account_info(123, 123, 77, self.bot, backend,
                                         self.state, username='alice')
            profile_screen = render.call_args.args[2]
            self.assertIn('Telegram username: @alice', profile_screen.lines)
            self.assertIn('🇱🇻 Latvia: AmneziaWG, VLESS', profile_screen.lines)
            await user.show_account_stats(123, 123, 77, 'p1', self.bot,
                                          backend, self.state)
            stats_screen = render.call_args.args[2]
        self.assertIn('Configs issued: 3', stats_screen.lines)
        self.assertIn('Profile created: 2026-09-29', stats_screen.lines)
        summary['expired'] = True
        with patch.object(user, 'render', new_callable=AsyncMock) as render:
            await user.show_account_profile(123, 123, 77, 'p1', self.bot,
                                            backend, self.state)
        self.assertIn('Status: Expired', render.call_args.args[2].lines)
        backend.member_profile_summary.assert_awaited_with(123, 'p1')

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

    async def test_awg_qr_uses_import_payload_and_returns_to_issuance(self):
        self.assertEqual(user.qr_payload('awg', 'vpn', 'vpn://fresh-config'),
                         'fresh-config')
        self.assertEqual(user.qr_payload('xray', 'tcp', 'vless://link'),
                         'vless://link')
        backend = SimpleNamespace(
            issuance=AsyncMock(return_value={'protocol': 'awg', 'transport': 'vpn',
                'profile_id': 'p1', 'node_key': 'lv1'}),
            artifact=AsyncMock(return_value={'content': 'vpn://fresh-config'}))
        self.bot.send_photo.return_value = SimpleNamespace(message_id=88)
        with patch.object(user, 'render', new_callable=AsyncMock) as render:
            await user.show_qr(123, 123, 77, 'issuance1', self.bot, backend,
                               self.state)
        back = render.call_args.args[3][0][0]
        action = user.actions[back.callback_data[2:]]
        self.assertEqual((action.name, action.args), ('qr_back', ('issuance1',)))

    async def test_member_action_error_hides_backend_code(self):
        self.query.from_user.username = 'alice'
        self.query.data = user.button(123, 'Open', 'account_profile', 'p1',
                                      'home').callback_data
        backend = SimpleNamespace(member_profile_summary=AsyncMock(
            side_effect=BackendError('grant_revoked', 403)))
        with patch.object(user, 'render', new_callable=AsyncMock) as render:
            await user.user_action_cb(self.query, self.bot, backend, self.state)
        screen = render.call_args.args[2]
        self.assertNotIn('grant_revoked', ' '.join(screen.lines))
        self.assertIn('access has changed', ' '.join(screen.lines))

    async def test_grants_are_saved_together_with_profile_revision(self):
        backend = SimpleNamespace(
            profile_grants=AsyncMock(return_value={'items': [
                {'node_key': 'lv1', 'protocol': 'awg'}]}),
            request=AsyncMock(return_value={'desired_revision': 7}),
            replace_grants=AsyncMock())
        with patch.object(admin_profiles, 'show_grant_protocols', new_callable=AsyncMock):
            await admin_profiles.change_grant(self.query, 'p1', 'lv1', 'xray',
                True, self.bot, backend, self.state)
        backend.replace_grants.assert_not_awaited()
        self.assertEqual(self.state_data['draft_grants'], [
            {'node_key': 'lv1', 'protocol': 'awg'},
            {'node_key': 'lv1', 'protocol': 'xray'}])
        self.query.data = 'admin_profile_grants_save:p1'
        with patch.object(admin_profiles, 'show_admin_profile', new_callable=AsyncMock):
            await admin_profiles.save_grants_cb(self.query, self.bot, backend, self.state)
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

    async def test_node_wizard_saves_then_queues_agent_from_persisted_connection(self):
        self.query.data = 'wizard_proto:done'
        self.state_data.update({
            'create_node_command_key': 'create-key',
            'rollout_command_key': 'rollout-key',
            'wizard_data': {'key': 'lv1', 'title': 'Latvia', 'region': 'EU',
                            'flag': '🇱🇻', 'public_host': 'lv1.example.com',
                            'protocols': ['awg', 'xray'], 'transport': 'ssh',
                            'ssh_target': 'root@lv1.example.com'}})
        backend = SimpleNamespace(create_node=AsyncMock(return_value={'key': 'lv1'}),
            rollout_agent=AsyncMock(return_value={'id': 'rollout-id'}),
            request=AsyncMock(return_value={'transport': 'ssh',
                'ssh_target': 'root@lv1.example.com'}))
        with patch.object(admin_nodes, 'render', new_callable=AsyncMock), \
             patch.object(admin_nodes, 'show_rollout_status', new_callable=AsyncMock):
            await admin_nodes.wizard_proto_cb(self.query, self.bot, backend, self.state)
            self.assertIsNone(backend.create_node.await_args)
            self.assertIsNone(backend.rollout_agent.await_args)
            await admin_nodes.wizard_save_cb(self.query, self.bot, backend, self.state)
            backend.rollout_agent.assert_not_awaited()
            await admin_nodes.wizard_setup_agent_cb(self.query, self.bot, backend, self.state)
        args, kwargs = backend.create_node.call_args
        self.assertEqual(args[0], 123)
        self.assertEqual(kwargs['command_key'], 'create-key')
        self.assertEqual(args[1]['xray_transports'], ['tcp', 'xhttp'])
        self.assertEqual(args[1]['transport'], 'ssh')
        self.assertEqual(args[1]['ssh_target'], 'root@lv1.example.com')
        backend.rollout_agent.assert_awaited_once_with(123, 'lv1', 'ssh',
            ssh_target='root@lv1.example.com', command_key='rollout-key')

    async def test_switching_node_to_ssh_requires_and_saves_target(self):
        self.query.data = 'node_connection_set:lv1:ssh'
        backend = SimpleNamespace(request=AsyncMock(return_value={
            'key': 'lv1', 'transport': 'local', 'ssh_target': None,
            'desired_revision': 4, 'settings': {}}), edit_node=AsyncMock())
        message = SimpleNamespace(from_user=SimpleNamespace(id=123), text='root@lv1.example.com',
            chat=SimpleNamespace(id=123, type='private'), delete=AsyncMock())
        with patch.object(admin_nodes, 'render', new_callable=AsyncMock):
            await admin_nodes.node_connection_set_cb(self.query, self.bot, backend, self.state)
            backend.edit_node.assert_not_awaited()
            await admin_nodes.process_node_edit(message, self.bot, backend, self.state)
        backend.edit_node.assert_awaited_once()
        self.assertEqual(backend.edit_node.await_args.args[:4],
                         (123, 'lv1', 4, {'transport': 'ssh',
                                        'ssh_target': 'root@lv1.example.com'}))

    async def test_protocol_toggle_uses_a_valid_idempotency_key(self):
        backend = SimpleNamespace(request=AsyncMock(return_value={
            'protocols': ['awg', 'xray'], 'xray_transports': ['tcp'],
            'settings': {}, 'desired_revision': 3}), edit_node=AsyncMock())
        with patch.object(admin_nodes, 'show_node_protocols', new_callable=AsyncMock):
            await admin_nodes.toggle_node_feature(123, 123, 77, 'lv1', 'xhttp',
                                                  True, self.bot, backend, self.state)
        args, kwargs = backend.edit_node.await_args
        self.assertEqual(args[:3], (123, 'lv1', 3))
        self.assertEqual(args[3]['xray_transports'], ['tcp', 'xhttp'])
        UUID(kwargs['command_key'])

    async def test_apply_stays_in_settings_and_handles_backend_failure(self):
        node = {'key': 'lv1', 'title': 'Latvia', 'region': 'EU', 'flag': '🇱🇻',
                'protocols': ['awg'], 'xray_transports': [], 'transport': 'local',
                'ssh_target': None, 'settings': {}, 'desired_revision': 3,
                'applied_revision': 2}
        backend = SimpleNamespace(request=AsyncMock(return_value=node),
            node_overview=AsyncMock(return_value={'state': 'changes_pending',
                'applied_revision': 2, 'desired_revision': 3,
                'ready': 0, 'access_total': 1, 'pending': 0,
                'failed': 0, 'attention': 1}),
            apply_node_settings=AsyncMock(side_effect=BackendError('agent_unavailable', 503)))
        with patch.object(admin_nodes, 'render', new_callable=AsyncMock) as render:
            await admin_nodes.show_node_settings(123, 123, 77, 'lv1',
                                                 self.bot, backend, self.state)
            settings_rows = render.call_args.args[3]
            self.assertTrue(any(button.callback_data.startswith('apply_node:')
                for row in settings_rows for button in row))
            await admin_nodes.show_admin_node(123, 123, 77, 'lv1',
                                              self.bot, backend, self.state)
            backend.node_overview.assert_awaited_once_with(123, 'lv1')
            card_rows = render.call_args.args[3]
            self.assertFalse(any(button.callback_data.startswith('apply_node:')
                for row in card_rows for button in row))
            result = await admin_nodes.apply_node(123, 123, 77, 'lv1',
                                                  self.bot, backend, self.state)
        self.assertFalse(result)

    async def test_probe_unavailable_shows_recovery_without_raw_rpc_error(self):
        backend = SimpleNamespace(node_runtime=AsyncMock(
            side_effect=BackendError('node_agent_unavailable', 503)))
        with patch.object(admin_nodes, 'render', new_callable=AsyncMock) as render:
            await admin_nodes.probe_node_cb(self.query, ProbeNodeCallback(node_key='lv1'),
                                            self.bot, backend, self.state)
        screen = render.call_args.args[2]
        self.assertIn('unreachable', ' '.join(screen.lines))
        self.assertNotIn('node_agent_unavailable', ' '.join(screen.lines))

    async def test_node_diagnostics_displays_agent_status_without_paths(self):
        self.query.data = 'node_diagnostics:lv1'
        backend = SimpleNamespace(node_diagnostics=AsyncMock(return_value={
            'node_key': 'lv1', 'docker': 'ok', 'runtime_root': 'ok',
            'xray_config': 'ok', 'awg_config': 'missing',
            'runtime_version': '0.4.3'}))
        with patch.object(admin_nodes, 'render', new_callable=AsyncMock) as render:
            await admin_nodes.node_diagnostics_cb(self.query, self.bot, backend, self.state)
        screen = render.call_args.args[2]
        self.assertIn('Docker: ready', screen.lines)
        self.assertIn('AWG config: missing', screen.lines)
        self.assertNotIn('/opt/', ' '.join(screen.lines))
        backend.node_diagnostics.assert_awaited_once_with(123, 'lv1')

    async def test_node_diagnostics_unavailable_has_recovery(self):
        self.query.data = 'node_diagnostics:lv1'
        backend = SimpleNamespace(node_diagnostics=AsyncMock(
            side_effect=BackendError('node_agent_unavailable', 503)))
        with patch.object(admin_nodes, 'render', new_callable=AsyncMock) as render:
            await admin_nodes.node_diagnostics_cb(self.query, self.bot, backend, self.state)
        screen = render.call_args.args[2]
        self.assertIn('unreachable', ' '.join(screen.lines))
        self.assertNotIn('node_agent_unavailable', ' '.join(screen.lines))

    async def test_node_maintenance_translates_state_and_cleanup_phase(self):
        self.state_data['locale'] = 'ru'
        backend = SimpleNamespace(node_maintenance=AsyncMock(return_value={
            'status': 'draining', 'verification_target': 'root@lv1.example.com',
            'pending_tasks': 1, 'blocked_tasks': 0, 'cleanup_phase': 'prepared',
            'revocations_complete': False}))
        with patch.object(admin_nodes, 'render', new_callable=AsyncMock) as render:
            await admin_nodes.show_node_maintenance(123, 123, 77, 'lv1',
                                                    self.bot, backend, self.state)
        screen = render.call_args.args[2]
        self.assertIn('Состояние: отключается', screen.lines)
        self.assertIn('Очистка runtime: подготовлено', screen.lines)

    async def test_request_list_uses_backend_cursor_and_search_controls(self):
        backend = SimpleNamespace(pending_access_requests=AsyncMock(return_value={
            'items': [{'id': 'r1', 'account_id': 'account-one',
                'first_name': 'Alex', 'last_name': None, 'username': 'alex',
                'telegram_user_id': 456}], 'next_cursor': 'next'}))
        with patch.object(admin_requests, 'render', new_callable=AsyncMock) as render:
            await admin_requests.render_request_page(123, 123, 77, self.bot,
                backend, self.state, 0)
        rows = render.call_args.args[3]
        callbacks = [button.callback_data for row in rows for button in row]
        self.assertIn('request_page:1', callbacks)
        self.assertIn('request_search', callbacks)
        backend.pending_access_requests.assert_awaited_once_with(123,
            cursor=None, search=None, limit=10)

    async def test_request_notification_has_direct_approve_and_reject_actions(self):
        backend = SimpleNamespace(me=AsyncMock(return_value={
            'role': 'admin', 'status': 'approved', 'locale': 'en'}))
        with patch.dict('os.environ', {'ADMIN_IDS': '123'}), \
             patch.object(admin_requests, 'send_notice', new_callable=AsyncMock) as send_notice:
            await admin_requests.notify_admins(self.bot, backend, 'request-id')
        markup = send_notice.call_args.args[3]
        callbacks = [button.callback_data for row in markup.inline_keyboard
                     for button in row]
        self.assertEqual(len(callbacks), 3)
        self.assertTrue(any(value.startswith('notification_decide:') and 'approve' in value
                            for value in callbacks))
        self.assertTrue(any(value.startswith('notification_decide:') and 'reject' in value
                            for value in callbacks))

    async def test_deciding_last_request_returns_to_admin_menu(self):
        self.state_data['locale'] = 'en'
        backend = SimpleNamespace(
            pending_access_request=AsyncMock(return_value={
                'id': 'request-id', 'account_id': 'account-id', 'status': 'pending',
                'locale': 'en'}),
            pending_access_requests=AsyncMock(return_value={
                'items': [], 'next_cursor': None}),
            request=AsyncMock(side_effect=[{'account_id': 'account-id'},
                BackendError('resource_not_found', 404)]))
        with patch.object(admin_requests, 'render', new_callable=AsyncMock), \
             patch.object(user, 'render', new_callable=AsyncMock) as render:
            await admin_requests.decide_cb(self.query,
                admin_requests.DecideCallback(request_id='request-id',
                                              decision='approve'),
                self.bot, backend, self.state)
        self.assertEqual(render.call_args.args[2].title, 'Admin panel')

    async def test_failed_node_cleanup_has_recoverable_screen(self):
        from telegram_client.routers.callbacks import CleanupStepCallback
        backend = SimpleNamespace(cleanup_node_step=AsyncMock(
            side_effect=BackendError('node_agent_unavailable', 503)))
        with patch.object(admin_nodes, 'render', new_callable=AsyncMock) as render:
            await admin_nodes.cleanup_step_cb(self.query,
                CleanupStepCallback(node_key='lv1', expected_phase='prepared'),
                self.bot, backend, self.state)
        screen = render.call_args.args[2]
        callbacks = [button.callback_data for row in render.call_args.args[3]
                     for button in row]
        self.assertNotIn('node_agent_unavailable', ' '.join(screen.lines))
        self.assertTrue(any(value.startswith('node_maintenance:') for value in callbacks))

    async def test_agent_setup_success_links_to_settings_without_dead_runtime_button(self):
        backend = SimpleNamespace(agent_rollout=AsyncMock(return_value={
            'id': 'task1', 'node_key': 'lv1', 'status': 'succeeded'}))
        with patch.object(admin_nodes, 'render', new_callable=AsyncMock) as render:
            await admin_nodes.show_rollout_status(123, 123, 77, 'task1',
                                                  self.bot, backend, self.state)
        callbacks = [button.callback_data for row in render.call_args.args[3]
                     for button in row]
        self.assertTrue(any(value.startswith('node_settings:') for value in callbacks))
        self.assertFalse(any(value.startswith('refresh_runtime:') for value in callbacks))

    async def test_install_menu_only_offers_apply_when_settings_are_complete(self):
        self.query.data = 'bootstrap_menu:lv1'
        node = {'key': 'lv1', 'transport': 'local', 'ssh_target': None}
        overview = {'state': 'not_installed', 'settings_complete': False}
        backend = SimpleNamespace(request=AsyncMock(return_value=node),
                                  node_overview=AsyncMock(return_value=overview))
        with patch.object(admin_nodes, 'render', new_callable=AsyncMock) as render:
            await admin_nodes.bootstrap_menu_cb(self.query, self.bot, backend, self.state)
            callbacks = [button.callback_data for row in render.call_args.args[3]
                         for button in row]
            self.assertFalse(any(value.startswith('apply_node:') for value in callbacks))
            overview['settings_complete'] = True
            await admin_nodes.bootstrap_menu_cb(self.query, self.bot, backend, self.state)
            callbacks = [button.callback_data for row in render.call_args.args[3]
                         for button in row]
            self.assertTrue(any(value.startswith('apply_node:') for value in callbacks))
            overview['state'] = 'applied_unverified'
            await admin_nodes.bootstrap_menu_cb(self.query, self.bot, backend, self.state)
            callbacks = [button.callback_data for row in render.call_args.args[3]
                         for button in row]
            self.assertFalse(any(value.startswith('apply_node:') for value in callbacks))
            self.assertTrue(any(value.startswith('probe_node:') for value in callbacks))

    async def test_node_wizard_navigation_has_back_and_paired_forward_buttons(self):
        self.state_data['wizard_data'] = {'key': 'lv1', 'title': 'Latvia',
            'region': 'EU', 'flag': '🇱🇻', 'transport': 'ssh',
            'ssh_target': 'root@lv1.example.com', 'public_host': 'lv1.example.com',
            'protocols': ['awg']}
        with patch.object(admin_nodes, 'render', new_callable=AsyncMock) as render:
            await admin_nodes.render_wizard_step(123, self.bot, self.state, 'key', 77)
            self.assertEqual([[button.callback_data for button in row]
                for row in render.call_args.args[3]], [['admin_nodes']])
            await admin_nodes.render_wizard_step(123, self.bot, self.state, 'flag', 77)
            self.assertEqual([[button.callback_data for button in row]
                for row in render.call_args.args[3]],
                [['wizard_back:region', 'wizard_skip_flag']])
            await admin_nodes.render_wizard_step(123, self.bot, self.state, 'public_host', 77)
            self.assertEqual(render.call_args.args[3][0][0].callback_data,
                             'wizard_back:target')
            await admin_nodes.render_wizard_protocols(123, self.bot, self.state, 77)
            self.assertEqual([button.callback_data for button in
                render.call_args.args[3][-1]],
                ['wizard_back:public_host', 'wizard_proto:done'])
            await admin_nodes.render_wizard_summary(123, self.bot, self.state, 77)
            self.assertEqual([button.callback_data for button in
                render.call_args.args[3][0]], ['wizard_proto:back', 'wizard_save'])

    async def test_profile_wizard_back_keeps_draft_and_next_opens_review(self):
        backend = SimpleNamespace(admin_nodes=AsyncMock(return_value={'items': [
            {'key': 'n1', 'title': 'Node 1', 'protocols': ['awg', 'xray']},
            {'key': 'n2', 'title': 'Node 2', 'protocols': ['xray']}]}))
        with patch.object(admin_profiles, 'render', new_callable=AsyncMock) as render:
            await admin_profiles.new_profile_cb(self.query,
                NewProfileCallback(account_id='account1'), self.bot, self.state)
            self.assertTrue(render.call_args.args[3][0][0].callback_data.startswith('account:'))
            self.state_data.update(draft_profile_name='Alice', draft_grants=[
                {'node_key': 'n1', 'protocol': 'awg'},
                {'node_key': 'n2', 'protocol': 'xray'}])
            await admin_profiles.show_create_nodes(123, 123, 77,
                                                   self.bot, backend, self.state)
            self.assertEqual([button.callback_data for button in
                render.call_args.args[3][-1]],
                ['profile_draft_name', 'profile_draft_review'])
            self.query.data = 'profile_draft_node:0'
            await admin_profiles.draft_node_cb(self.query, self.bot, self.state)
            self.assertEqual([button.callback_data for button in
                render.call_args.args[3][-1]],
                ['profile_draft_nodes', 'profile_draft_review'])
            self.query.data = 'profile_draft_toggle:xray'
            await admin_profiles.draft_toggle_cb(self.query, self.bot, self.state)
            self.query.data = 'profile_draft_nodes'
            await admin_profiles.draft_nodes_cb(self.query, self.bot,
                                               backend, self.state)
            self.assertIn({'node_key': 'n1', 'protocol': 'xray'},
                          self.state_data['draft_grants'])
            self.assertIsNone(self.state_data['draft_node_key'])
            self.query.data = 'profile_draft_review'
            await admin_profiles.draft_review_cb(self.query, self.bot, self.state)
            self.assertEqual([button.callback_data for button in
                render.call_args.args[3][0]],
                ['profile_draft_nodes', 'profile_draft_save'])
            self.assertIn('Node 1', ' '.join(render.call_args.args[2].lines))

    async def test_profile_wizard_first_back_clears_draft_at_account_card(self):
        self.state_data['locale'] = 'ru'
        backend = SimpleNamespace(request=AsyncMock(return_value={
            'id': 'account1', 'telegram_user_id': 123,
            'status': 'approved', 'role': 'admin'}))
        with patch.object(admin_profiles, 'render', new_callable=AsyncMock) as render:
            await admin_profiles.new_profile_cb(self.query,
                NewProfileCallback(account_id='account1'), self.bot, self.state)
            self.state_data['draft_profile_name'] = 'Alice'
            await admin_profiles.account_cb(self.query,
                admin_profiles.AccountCallback(account_id='account1'),
                self.bot, backend, self.state)
        self.assertEqual(self.state_data, {'locale': 'ru'})
        self.assertTrue(render.call_args.args[3][-1][0].callback_data.startswith('accounts'))
