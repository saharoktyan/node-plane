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
            self.assertTrue(screen.sections[0].collapsed)
            self.assertEqual(screen.sections[1].title, '')
            self.assertIn('@operator', screen.sections[1].sections[0].title)
            self.assertTrue(screen.sections[1].sections[0].collapsed)
            self.assertIn('vps.example', '\n'.join(screen.sections[1].sections[0].lines))
            self.assertIn(event['action'], screen.sections[1].sections[0].lines)
            self.assertTrue(screen.sections[1].sections[1].lines[0].startswith('POST updates/run · '))
            self.assertFalse(screen.sections[1].divider_after)
            self.assertEqual(rows[-1][0].callback_data, recovery.RecoveryPage().pack())

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
