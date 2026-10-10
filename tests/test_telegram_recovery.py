from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from telegram_client.routers import admin_recovery as recovery
from tests import test_telegram_client as fixture


class RecoveryScreenTests(IsolatedAsyncioTestCase):
    setUp = fixture.TelegramFlowTests.setUp

    async def test_audit_is_rich_in_both_languages_with_collapsed_attribution(self):
        event = {'occurred_at': '2026-10-07T12:00:00+00:00', 'account_label': '@operator',
            'action': 'POST /api/v1/system/updates/run', 'phase': 'completed', 'http_status': 202,
            'ssh_user': 'deploy', 'device_fingerprint': 'SHA256:' + 'a' * 43,
            'account_id': str(uuid4()), 'session_id': str(uuid4()),
            'request_id': str(uuid4()), 'command_id': str(uuid4()),
            'target': 'root@[vps.example]:22', 'key_fingerprint': 'SHA256:' + 'b' * 43,
            'outcome': 'succeeded'}
        backend = SimpleNamespace(request=AsyncMock(return_value={'items': [event],
            'total': 1, 'offset': 0, 'page_size': 10}))
        for locale in ('en', 'ru'):
            self.state_data['locale'] = locale
            with patch.object(recovery, 'render', new_callable=AsyncMock) as render:
                await recovery.audit_page(self.query, recovery.AuditPage(), self.bot, backend, self.state)
            screen, rows = render.call_args.args[2:4]
            self.assertTrue(screen.rich(rows).blocks)
            entry = screen.sections[1]
            self.assertEqual(entry.title, '')
            self.assertTrue(entry.bold_first_line)
            self.assertIn('@operator', entry.lines[0])
            self.assertNotIn(event['account_id'], screen.plain())
            callback = entry.inline_rows[0][0].callback_data
            self.assertLessEqual(len(callback.encode()), 64)
            self.query.data = callback
            with patch.object(recovery, 'render', new_callable=AsyncMock) as detail:
                await recovery.recovery_detail(self.query, self.bot, self.state)
            self.assertIn(event['action'], detail.call_args.args[2].lines)
            self.assertIn('vps.example', '\n'.join(detail.call_args.args[2].lines))
            self.assertEqual(detail.call_args.args[3][0][0].callback_data, recovery.AuditPage().pack())
            self.assertEqual(rows[-1][0].callback_data, recovery.RecoveryPage().pack())

    async def test_diagnostics_prioritizes_problems_and_moves_long_details_out_of_list(self):
        backend = SimpleNamespace(request=AsyncMock(return_value={'errors': 1, 'warnings': 0, 'checks': [
            {'id': 'database', 'status': 'ok', 'detail': 'Connection verified'},
            {'id': 'worker', 'status': 'error', 'detail': 'Long failure explanation ' * 12}]}))
        with patch.object(recovery, 'render', new_callable=AsyncMock) as draw:
            await recovery.controller_diagnostics(self.query, self.bot, backend, self.state)
        screen, rows = draw.call_args.args[2:4]
        blocks = screen.rich(rows).blocks
        self.assertEqual(sum(b.type in {'heading', 'section_heading'} for b in blocks), 1)
        self.assertTrue(all(s.title == '' and s.bold_first_line for s in screen.sections))
        self.assertIn('Worker', screen.sections[0].inline_rows[0][0].text)
        self.assertNotIn('Connection verified', screen.plain())
        self.query.data = screen.sections[0].inline_rows[0][0].callback_data
        with patch.object(recovery, 'render', new_callable=AsyncMock) as detail:
            await recovery.recovery_detail(self.query, self.bot, self.state)
        self.assertIn('Long failure explanation ' * 12, detail.call_args.args[2].lines)
        self.assertEqual(detail.call_args.args[3][0][0].callback_data, recovery.RecoveryDiagnostics().pack())

    async def test_history_keeps_identifiers_in_details_and_returns_to_same_page(self):
        item = dict(created_at='2026-10-10T10:00:00Z', kind='settings', node_title='Latvia',
            action='recheck', outcome='confirmed', actor_id=str(uuid4()), operation_id=str(uuid4()))
        backend = SimpleNamespace(request=AsyncMock(return_value={'items': [item], 'offset': 10, 'total': 11, 'page_size': 10}))
        with patch.object(recovery, 'render', new_callable=AsyncMock) as draw:
            await recovery.recovery_history(self.query, recovery.RecoveryHistory(offset=10), self.bot, backend, self.state)
        screen, rows = draw.call_args.args[2:4]
        self.assertTrue(screen.sections[0].bold_first_line)
        self.assertIn('Latvia', screen.plain())
        self.assertNotIn(item['operation_id'], screen.plain())
        self.query.data = screen.sections[0].inline_rows[0][0].callback_data
        with patch.object(recovery, 'render', new_callable=AsyncMock) as detail:
            await recovery.recovery_detail(self.query, self.bot, self.state)
        self.assertIn(item['operation_id'], detail.call_args.args[2].lines)
        self.assertEqual(detail.call_args.args[3][0][0].callback_data, recovery.RecoveryHistory(offset=10).pack())
        self.assertTrue(screen.rich(rows).blocks)

    async def test_audit_error_filter_and_pagination_preserve_filter(self):
        backend = SimpleNamespace(request=AsyncMock(return_value={'items': [], 'offset': 10, 'total': 25, 'page_size': 10}))
        with patch.object(recovery, 'render', new_callable=AsyncMock) as draw:
            await recovery.audit_page(self.query, recovery.AuditPage(offset=10, errors=1), self.bot, backend, self.state)
        backend.request.assert_awaited_once_with('GET', '/api/v1/system/workstation-audit?offset=10&errors_only=true',
            telegram_user_id=self.query.from_user.id)
        rows = draw.call_args.args[3]
        self.assertEqual([b.callback_data for b in rows[0]], [recovery.AuditPage(errors=1).pack(), recovery.AuditPage(offset=20, errors=1).pack()])

    async def test_ssh_key_separate_message_preserves_menu_and_can_be_closed(self):
        from telegram_client.routers import admin_settings
        self.bot.send_message = AsyncMock()
        self.bot.delete_message = AsyncMock()
        key = 'ssh-ed25519 YWJj tester'
        backend = SimpleNamespace(request=AsyncMock(return_value={'public_key': key}))
        with patch.object(admin_settings, 'render', new_callable=AsyncMock) as draw:
            await admin_settings.ssh_key_cb(self.query, self.bot, backend, self.state)
        screen, rows = draw.call_args.args[2:4]
        blocks = screen.rich(rows).blocks
        key_index = next(i for i,b in enumerate(blocks) if b.type == 'paragraph' and 'YWJj' in str(b.text))
        link_index = next(i for i,b in enumerate(blocks) if 'ssh_key_plain' in str(b))
        self.assertGreater(link_index, key_index)
        old_state = dict(self.state_data)
        await admin_settings.ssh_key_plain_cb(self.query, self.bot, backend, self.state)
        sent = self.bot.send_message.call_args.kwargs
        self.assertEqual(sent['text'], key)
        self.assertIsNone(sent['parse_mode'])
        self.assertEqual(sent['entities'][0].type, 'code')
        self.assertEqual(sent['reply_markup'].inline_keyboard[0][0].callback_data, 'ssh_key_plain_close')
        self.assertEqual(self.state_data, old_state)
        await admin_settings.ssh_key_plain_close_cb(self.query, self.bot)
        self.bot.delete_message.assert_awaited_with(self.query.message.chat.id, self.query.message.message_id)

    async def test_confirmation_precedes_cancel_and_uses_existing_contract(self):
        identity = str(uuid4())
        for locale in ('en', 'ru'):
            self.state_data['locale'] = locale
            backend = SimpleNamespace(request=AsyncMock(return_value={'status': 'cancelled'}))
            action = recovery.RecoveryAction(kind='update', id=identity, action='cancel')
            self.assertLessEqual(len(action.pack().encode()), 64)
            with patch.object(recovery, 'render', new_callable=AsyncMock) as render:
                await recovery.recovery_action(self.query, action, self.bot, backend, self.state)
            backend.request.assert_not_called()
            screen, rows = render.call_args.args[2:4]
            self.assertTrue(screen.rich(rows).blocks)
            self.assertEqual(rows[0][0].style, 'danger')
            with patch.object(recovery, 'show', new_callable=AsyncMock):
                await recovery.recovery_action(self.query, action.model_copy(update={'confirm': 1}), self.bot, backend, self.state)
            backend.request.assert_awaited_once_with('POST',
                f'/api/v1/system/updates/jobs/{identity}/cancel', telegram_user_id=self.query.from_user.id)

    async def test_no_operations_still_has_rich_screen_and_unknown_actions_never_dispatch(self):
        backend = SimpleNamespace(request=AsyncMock(return_value={'items': [], 'total': 0,
            'offset': 0, 'page_size': 10, 'maintenance_active': False}))
        with patch.object(recovery, 'render', new_callable=AsyncMock) as render:
            await recovery.show(self.query, self.bot, backend, self.state)
        screen, rows = render.call_args.args[2:4]
        self.assertTrue(screen.rich(rows).blocks)
        self.assertEqual(rows[-1][0].callback_data, 'admin_settings')
        backend.request.reset_mock()
        with patch.object(recovery, 'show', new_callable=AsyncMock):
            await recovery.recovery_action(self.query,
                recovery.RecoveryAction(kind='profile', id=str(uuid4()), action='unlock', confirm=1),
                self.bot, backend, self.state)
        backend.request.assert_not_called()

    async def test_settings_and_profile_recovery_have_specific_confirmation_and_route(self):
        identity = str(uuid4())
        for kind in ('settings','profile','bootstrap'):
            action = recovery.RecoveryAction(kind=kind,id=identity,action='resolve')
            self.assertLessEqual(len(action.pack().encode()),64)
            backend = SimpleNamespace(request=AsyncMock(return_value={'status':'superseded','replacement_id':str(uuid4())}))
            with patch.object(recovery,'render',new_callable=AsyncMock) as render:
                await recovery.recovery_action(self.query,action,self.bot,backend,self.state)
            backend.request.assert_not_called()
            self.assertNotIn('recovery.explain.',str(render.call_args.args[2]))
            with patch.object(recovery,'show',new_callable=AsyncMock) as show:
                await recovery.recovery_action(self.query,action.model_copy(update={'confirm':1}),self.bot,backend,self.state)
            self.assertIn(backend.request.call_args.args[1], (f'/api/v1/system/recovery/{kind}/{identity}/resolve',))
            self.assertIn(backend.request.return_value['replacement_id'],show.call_args.kwargs['note'])

    async def test_inventory_shows_reason_hint_and_keeps_identifiers_in_details(self):
        identity = str(uuid4())
        backend = SimpleNamespace(request=AsyncMock(return_value={'items':[{'id':identity,'kind':'agent','node_key':'lv1','status':'blocked','actions':['recheck'],'error_code':'ssh_authentication','next_step':'agent'}], 'total':1,'offset':0,'page_size':10,'maintenance_active':False}))
        for locale in ('ru','en'):
            self.state_data['locale']=locale
            with patch.object(recovery,'render',new_callable=AsyncMock) as render:
                await recovery.show(self.query,self.bot,backend,self.state)
            screen,rows=render.call_args.args[2:4]
            self.assertNotIn(identity,screen.sections[0].lines)
            self.assertEqual(screen.sections[0].sections[0].lines,(identity,))
            self.assertTrue(screen.sections[0].sections[0].collapsed)
            self.assertNotIn('recovery.',str(screen.sections[0].lines))
            self.assertTrue(any(button.callback_data == recovery.RecoveryDiagnostics().pack() for row in rows for button in row))
