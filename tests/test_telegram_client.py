"""Exercise the Telegram paths that connect a person, node, and config."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch
from uuid import UUID

from aiogram import Dispatcher

from telegram_client import main
from telegram_client.backend import BackendError, BackendClient
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
    async def test_awg_preset_selection_updates_desired_settings_only(self):
        backend = SimpleNamespace(request=AsyncMock(return_value={
            'desired_revision': 4, 'settings': {'awg_port': 51820, 'awg_i1_preset': 'quic'}}),
            edit_node=AsyncMock())
        from telegram_client.routers import admin_node_tools
        for preset in ('quic', 'dns', 'chaos'):
            self.query.data = f'node_awg_preset:{preset}:lv1'
            with patch.object(admin_node_tools, 'show_section', new_callable=AsyncMock):
                await admin_nodes.select_awg_preset(self.query, self.bot, backend, self.state)
            body = backend.edit_node.call_args.args[3]
            self.assertEqual(body['settings'], {'awg_port': 51820, 'awg_i1_preset': preset})
        self.assertEqual(backend.request.call_count, 3)

    async def test_node_action_preserves_command_idempotency_header(self):
        client = BackendClient.__new__(BackendClient)
        client.request = AsyncMock(return_value={'id': 'job'})
        await client.node_action(101, 'lv1', 'reinstall_clean', 4, 'stable-key')
        self.assertTrue(client.request.call_args.kwargs['command'])
        self.assertEqual(client.request.call_args.kwargs['command_key'], 'stable-key')

    async def test_backup_confirmation_preserves_key_and_action_label(self):
        from telegram_client.routers import admin_backups
        backend = SimpleNamespace(backup_command=AsyncMock(return_value={'id': 'job'}),
            backup_job=AsyncMock(return_value={'status': 'awaiting_executor', 'action': 'create', 'result': None}))
        self.state_data['locale'] = 'en'
        self.query.data = 'backup_create'
        with patch.object(admin_backups, 'render', new_callable=AsyncMock) as draw:
            await admin_backups.backup_cb(self.query, self.bot, backend, self.state)
            self.assertEqual(draw.call_args.args[3][0][1].text, user.tr('en', 'backups.create'))
            draft = self.state_data['backup_draft']
            self.query.data = 'backup_submit:' + draft['nonce']
            await admin_backups.backup_cb(self.query, self.bot, backend, self.state)
            await admin_backups.backup_cb(self.query, self.bot, backend, self.state)
        self.assertEqual(backend.backup_command.call_args_list[0].args[2], draft['key'])
        self.assertEqual(backend.backup_command.call_args_list[1].args[2], draft['key'])

    async def test_backup_incompatible_snapshot_has_no_restore_button_and_keeps_page(self):
        from telegram_client.routers import admin_backups
        backend = SimpleNamespace(backup_detail=AsyncMock(return_value={
            'id': 'snapshot', 'created_at': '2026-09-30', 'app_version': '0.4.3',
            'profiles': 2, 'nodes': 1, 'compatible': False}))
        self.state_data['backup_offset'] = 8
        self.query.data = 'backup_detail:snapshot'
        with patch.object(admin_backups, 'render', new_callable=AsyncMock) as draw:
            await admin_backups.backup_cb(self.query, self.bot, backend, self.state)
        callbacks = [b.callback_data for row in draw.call_args.args[3] for b in row]
        self.assertEqual(callbacks, ['backup_list:8'])

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

    async def test_update_actions_hidden_when_live_commits_are_current(self):
        from telegram_client.routers import admin_updates
        backend = SimpleNamespace(update_rollout=AsyncMock(return_value={
            'desired_version': '0.4.3', 'desired_commit': 'a' * 40,
            'driver_status': 'current', 'driver': {'commit': 'a' * 40},
            'nodes': [{'title': 'Latvia', 'agent_status': 'current', 'runtime_status': 'current'}],
            'agents_required': False, 'runtimes_required': False, 'latest_job': None}))
        with patch.object(admin_updates, 'render', new_callable=AsyncMock) as draw:
            await admin_updates.show_fleet(self.query, self.bot, backend, self.state)
        callbacks = [b.callback_data for row in draw.call_args.args[3] for b in row]
        self.assertNotIn('ufleet_confirm:agents', callbacks)
        self.assertNotIn('ufleet_confirm:runtimes', callbacks)
        backend.update_rollout.return_value['agents_required'] = True
        with patch.object(admin_updates, 'render', new_callable=AsyncMock) as draw:
            await admin_updates.show_fleet(self.query, self.bot, backend, self.state)
        self.assertIn('ufleet_confirm:agents', [b.callback_data for row in draw.call_args.args[3] for b in row])

    async def test_announcement_double_tap_reuses_command_key(self):
        from telegram_client.routers import admin_announcements
        self.state_data['announcement_draft']={'text':'Hello','nonce':'test','key':'stable-key'}
        self.query.data='announce_send:test'
        backend=SimpleNamespace(announcement_create=AsyncMock(return_value={'id':'job'}),
            announcement_status=AsyncMock(return_value={'total':1,'status':'running',
                'counts':dict(queued=1,claimed=0,sent=0,failed=0,unknown=0,skipped=0)}))
        with patch.object(admin_announcements,'render',new_callable=AsyncMock):
            await admin_announcements.announcement_cb(self.query,self.bot,backend,self.state)
            await admin_announcements.announcement_cb(self.query,self.bot,backend,self.state)
        self.assertEqual([call.args[2] for call in backend.announcement_create.call_args_list],['stable-key','stable-key'])

    async def test_announcement_edit_back_and_preview_share_row(self):
        from telegram_client.routers import admin_announcements
        self.state_data['announcement_draft']={'text':'Hello','nonce':'test','key':'stable-key'}
        self.query.data='announce_edit'
        backend=SimpleNamespace(me=AsyncMock(return_value={'permissions':['settings.manage']}))
        with patch.object(admin_announcements,'render',new_callable=AsyncMock) as draw:
            await admin_announcements.announcement_cb(self.query,self.bot,backend,self.state)
        self.assertEqual([b.callback_data for b in draw.call_args.args[3][0]],['announce_menu','announce_preview'])

    async def test_announcement_transport_sends_silently_and_acknowledges(self):
        from telegram_client.announcement_delivery import deliver_one
        bot=SimpleNamespace(send_rich_message=AsyncMock())
        backend=SimpleNamespace(announcement_claim=AsyncMock(return_value={'delivery':
            {'id':'delivery','telegram_user_id':102,'text':'Hello','locale':'ru','silent':True}}),
            announcement_ack=AsyncMock())
        self.assertTrue(await deliver_one(bot,backend))
        self.assertTrue(bot.send_rich_message.call_args.kwargs['disable_notification'])
        self.assertEqual(backend.announcement_ack.call_args.args[2],'sent')
        self.assertEqual(backend.announcement_claim.call_args.args[0],backend.announcement_ack.call_args.args[1])

    async def test_announcement_transport_does_not_replay_ambiguous_send(self):
        from telegram_client.announcement_delivery import deliver_one
        bot=SimpleNamespace(send_rich_message=AsyncMock(side_effect=TimeoutError()))
        backend=SimpleNamespace(announcement_claim=AsyncMock(return_value={'delivery':
            {'id':'delivery','telegram_user_id':102,'text':'Hello','silent':False}}),
            announcement_ack=AsyncMock())
        await deliver_one(bot,backend)
        self.assertEqual(bot.send_rich_message.await_count,1)
        self.assertEqual(backend.announcement_ack.call_args.args[2],'unknown')

    async def test_alert_policy_selected_interval_and_back_navigation(self):
        from telegram_client.routers import admin_alerts
        backend=SimpleNamespace(alerts_overview=AsyncMock(return_value={
            'enabled':True,'notify_resolved':True,'interval_minutes':15,'active_count':0,'active':[],
            'last_scan':None,'delivery_counts':dict(queued=0,claimed=0,sent=0,failed=0,unknown=0,skipped=0)}))
        self.query.data='alerts'
        self.state_data['locale']='en'
        with patch.object(admin_alerts,'render',new_callable=AsyncMock) as draw:
            await admin_alerts.alerts_cb(self.query,self.bot,backend,self.state)
        rows=draw.call_args.args[3]
        self.assertEqual(rows[1][1].text,'✅ 15 min')
        self.assertTrue(rows[-1][0].callback_data.startswith('admin_settings'))

    async def test_active_alert_pagination_and_localized_transport(self):
        from telegram_client.routers import admin_alerts
        from telegram_client.alert_delivery import alert_screen
        backend=SimpleNamespace(alerts_overview=AsyncMock(return_value={'active':[
            {'title':str(i),'kind':'xray_down'} for i in range(9)]}))
        self.query.data='alerts_active:8'
        with patch.object(admin_alerts,'render',new_callable=AsyncMock) as draw:
            await admin_alerts.alerts_cb(self.query,self.bot,backend,self.state)
        rows=draw.call_args.args[3]
        self.assertEqual([button.callback_data for button in rows[0]],['alerts_active:0'])
        screen=alert_screen({'locale':'ru','event':{'kind':'node_unreachable','resolved':True,
            'at':'2026-09-30T10:00:00+00:00','payload':{'node_title':'Moscow'}}})
        self.assertIn('устранена',screen.title)
        self.assertIn('Moscow',screen.plain())

    async def test_update_confirmation_uses_same_key_on_double_tap(self):
        from telegram_client.routers import admin_updates
        backend = SimpleNamespace(run_update=AsyncMock(return_value={'id': 'job'}),
            update_job=AsyncMock(return_value={'id': 'job', 'status': 'awaiting_executor', 'items': []}))
        with patch.object(admin_updates, 'render', new_callable=AsyncMock):
            await admin_updates.confirm(self.query, self.bot, self.state,
                {'kind': 'version', 'branch': 'dev', 'target_ref': 'v0.4.3'}, ['Confirm'])
            draft = self.state_data['update_draft']
            self.query.data = 'update_submit:' + draft['nonce']
            await admin_updates.update_tools_cb(self.query, self.bot, backend, self.state)
            await admin_updates.update_tools_cb(self.query, self.bot, backend, self.state)
        self.assertEqual(backend.run_update.call_args_list[0].args[2], draft['key'])
        self.assertEqual(backend.run_update.call_args_list[1].args[2], draft['key'])

    async def test_update_catalog_pagination_and_blocked_selection(self):
        from telegram_client.routers import admin_updates
        backend = SimpleNamespace(update_versions=AsyncMock(return_value={
            'items': [{'version': '0.3.0', 'ref': 'v0.3.0', 'allowed': False,
                       'action': 'blocked', 'reason': 'pre1_minor_downgrade_blocked'}],
            'branch': 'dev', 'offset': 8, 'next_offset': 16, 'total': 20, 'status': 'ok'}),
            run_update=AsyncMock())
        with patch.object(admin_updates, 'render', new_callable=AsyncMock) as draw:
            await admin_updates.show_versions(self.query, self.bot, backend, self.state, 8)
            callbacks = [b.callback_data for row in draw.call_args.args[3] for b in row]
            self.assertIn('uv_page:0', callbacks)
            self.assertIn('uv_page:16', callbacks)
            self.query.data = 'uv_select:' + self.state_data['update_catalog_nonce'] + ':0'
            await admin_updates.update_tools_cb(self.query, self.bot, backend, self.state)
        backend.run_update.assert_not_called()
        self.assertNotIn('update_draft', self.state_data)

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
                'profile_id': 'p1', 'node_key': 'lv1', 'protocol': 'awg',
                'transport': 'vpn'}),
            artifact=AsyncMock(return_value={'filename': 'Latvia.vpn',
                'content': 'vpn://fresh-config'}))
        with patch.object(user, 'render', new_callable=AsyncMock) as render:
            await user.show_issuance(123, 123, 77, 'issuance1',
                                     self.bot, backend, self.state)
        attachment = self.bot.send_document.call_args.args[1]
        self.assertEqual(attachment.filename, 'Latvia.vpn')
        self.assertEqual(attachment.data, b'vpn://fresh-config')
        screen = render.call_args.args[2]
        self.assertEqual(screen.details_title, 'AmneziaWG link')
        self.assertEqual(screen.details_lines, ('vpn://fresh-config',))
        self.assertIn('Import the .vpn file', screen.lines[0])

    async def test_issuance_poll_does_not_replace_screen_after_navigation(self):
        async def completed_after_back(*args):
            await self.state.update_data(issuance_poll_token=None)
            return {'status': 'succeeded'}
        backend = SimpleNamespace(
            issue=AsyncMock(return_value={'id': 'issuance1'}),
            issuance=AsyncMock(side_effect=completed_after_back))
        with patch.object(user, 'render', new_callable=AsyncMock) as render, \
             patch.object(user, 'show_issuance', new_callable=AsyncMock) as show_result:
            await user.issue(123, 123, 77, 'p1', 'lv1', 'awg', 'vpn',
                             self.bot, backend, self.state)
        self.assertEqual(render.await_count, 1)
        show_result.assert_not_awaited()
        self.bot.send_document.assert_not_awaited()

    async def test_artifact_read_does_not_deliver_after_navigation(self):
        async def artifact_after_back(*args):
            await self.state.update_data(issuance_poll_token=None)
            return {'filename': 'config.vpn', 'content': 'vpn://fresh-config'}
        backend = SimpleNamespace(
            issuance=AsyncMock(return_value={'status': 'succeeded',
                'profile_id': 'p1', 'node_key': 'lv1', 'protocol': 'awg',
                'transport': 'vpn'}),
            artifact=AsyncMock(side_effect=artifact_after_back))
        with patch.object(user, 'render', new_callable=AsyncMock) as render:
            await user.show_issuance(123, 123, 77, 'issuance1',
                                     self.bot, backend, self.state)
        render.assert_not_awaited()
        self.bot.send_document.assert_not_awaited()

    async def test_qr_artifact_read_after_back_does_not_send_photo(self):
        async def artifact_after_back(*args):
            await self.state.update_data(issuance_poll_token=None)
            return {'content': 'vpn://fresh-config'}
        backend = SimpleNamespace(
            issuance=AsyncMock(return_value={'protocol': 'awg', 'transport': 'vpn'}),
            artifact=AsyncMock(side_effect=artifact_after_back))
        with patch.object(user, 'render', new_callable=AsyncMock) as render:
            await user.show_qr(123, 123, 77, 'issuance1',
                               self.bot, backend, self.state)
        render.assert_not_awaited()
        self.bot.send_photo.assert_not_awaited()

    async def test_qr_photo_sent_after_back_is_deleted(self):
        async def photo_after_back(*args):
            await self.state.update_data(issuance_poll_token=None)
            return SimpleNamespace(message_id=88)
        self.bot.send_photo.side_effect = photo_after_back
        self.bot.delete_message = AsyncMock()
        backend = SimpleNamespace(
            issuance=AsyncMock(return_value={'protocol': 'awg', 'transport': 'vpn'}),
            artifact=AsyncMock(return_value={'content': 'vpn://fresh-config'}))
        with patch.object(user, 'render', new_callable=AsyncMock) as render:
            await user.show_qr(123, 123, 77, 'issuance1',
                               self.bot, backend, self.state)
        render.assert_not_awaited()
        self.bot.delete_message.assert_awaited_once_with(123, 88)

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
            node_services=AsyncMock(side_effect=BackendError('node_agent_unavailable', 503)),
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
        backend = SimpleNamespace(
            me=AsyncMock(return_value={
                'role': 'admin', 'status': 'approved', 'locale': 'en'}),
            access_request_policy=AsyncMock(return_value={'notify_requests': True}))
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

    async def test_request_notification_respects_admin_preference(self):
        backend = SimpleNamespace(
            me=AsyncMock(return_value={
                'role': 'admin', 'status': 'approved', 'locale': 'en'}),
            access_request_policy=AsyncMock(return_value={'notify_requests': False}))
        with patch.dict('os.environ', {'ADMIN_IDS': '123'}), \
             patch.object(admin_requests, 'send_notice', new_callable=AsyncMock) as send_notice:
            await admin_requests.notify_admins(self.bot, backend, 'request-id')
        send_notice.assert_not_awaited()

    async def test_deciding_last_request_returns_to_admin_menu(self):
        self.state_data['locale'] = 'en'
        backend = SimpleNamespace(
            pending_access_request=AsyncMock(return_value={
                'id': 'request-id', 'account_id': 'account-id', 'status': 'pending',
                'locale': 'en'}),
            pending_access_requests=AsyncMock(return_value={
                'items': [], 'next_cursor': None}),
            bot_title=AsyncMock(return_value={'title': 'Configured title'}),
            request=AsyncMock(side_effect=[{'account_id': 'account-id'},
                BackendError('resource_not_found', 404)]))
        with patch.object(admin_requests, 'render', new_callable=AsyncMock), \
             patch.object(user, 'render', new_callable=AsyncMock) as render:
            await admin_requests.decide_cb(self.query,
                admin_requests.DecideCallback(request_id='request-id',
                                              decision='approve'),
                self.bot, backend, self.state)
        self.assertEqual(render.call_args.args[2].title, 'Configured title')

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

    async def test_install_menu_checks_docker_and_reusable_configs(self):
        from telegram_client.routers import admin_node_tools
        node = {'key': 'lv1', 'transport': 'local', 'ssh_target': None, 'protocols': ['awg', 'xray']}
        facts = {'docker': False, 'awg_config_valid': False, 'xray_config_valid': False}
        backend = SimpleNamespace(request=AsyncMock(return_value=node),
            node_overview=AsyncMock(return_value={'settings_complete': True}),
            node_services=AsyncMock(return_value=facts))
        with patch.object(admin_node_tools, 'render', new_callable=AsyncMock) as render:
            async def buttons():
                await admin_node_tools.show_install(123, 123, 77, 'lv1', self.bot, backend, self.state)
                return [b.callback_data for row in render.call_args.args[3] for b in row]
            self.assertIn('node_action:install_docker:lv1', await buttons())
            facts['docker'] = True
            self.assertIn('node_action:bootstrap:lv1', await buttons())
            self.assertNotIn('node_action:reinstall_keep:lv1', await buttons())
            facts['awg_config_valid'] = True
            self.assertIn('node_action:reinstall_clean:lv1', await buttons())
            self.assertNotIn('node_action:reinstall_keep:lv1', await buttons())
            facts['xray_config_valid'] = True
            self.assertIn('node_action:reinstall_keep:lv1', await buttons())
            backend.node_overview.return_value = {'settings_complete': False}
            self.assertFalse(any(v.startswith('node_action:') for v in await buttons()))

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
