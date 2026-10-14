"""Alert policy controls, affected-node navigation and recovery pagination."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.routers import admin_alerts as alerts
from tests import test_telegram_client as fixture


class AlertsRichTests(IsolatedAsyncioTestCase):
    setUp = fixture.TelegramFlowTests.setUp

    def overview(self):
        return dict(enabled=True, notify_resolved=False, interval_minutes=10,
            active_count=0, active=[], last_scan=None,
            delivery_counts=dict(queued=0, claimed=0, failed=1, unknown=2))

    async def test_saved_choices_and_conditional_attention_in_both_languages(self):
        value = self.overview()
        backend = SimpleNamespace(alerts_overview=AsyncMock(return_value=value))
        for locale in ('ru', 'en'):
            self.state_data['locale'] = locale
            for count in (0, 2):
                value['active_count'] = count
                self.query.data = 'alerts'
                with patch.object(alerts, 'render', new_callable=AsyncMock) as draw:
                    await alerts.alerts_cb(self.query, self.bot, backend, self.state)
                screen, rows = draw.call_args.args[2:4]
                self.assertEqual([b.style for b in screen.sections[0].rows[0]], ['primary', None])
                self.assertEqual([b.style for b in screen.sections[2].rows[0]], [None, 'primary'])
                callbacks = [b.callback_data for row in screen.fallback_rows(rows) for b in row]
                self.assertEqual('alerts_active:0' in callbacks, bool(count))
                self.assertTrue(screen.sections[-1].collapsed)
                self.assertEqual([b.type for b in screen.rich(rows).blocks[-2:]], ['divider', 'buttons'])

    async def test_status_refresh_preserves_open_policy_details(self):
        from telegram_client.i18n import tr
        self.state_data['locale'] = 'en'
        self.query.data = 'alerts'
        self.query.message.rich_message = SimpleNamespace(blocks=[SimpleNamespace(type='details',summary=tr('en','alerts.rich.details'),is_open=True)])
        backend = SimpleNamespace(alerts_overview=AsyncMock(return_value=self.overview()))
        with patch.object(alerts,'render',new_callable=AsyncMock) as draw:
            await alerts.alerts_cb(self.query,self.bot,backend,self.state)
        self.assertTrue(draw.call_args.args[2].sections[-1].is_open)

    async def test_repeated_choice_sets_same_value_and_invalid_preference_does_not_write(self):
        backend = SimpleNamespace(alerts_overview=AsyncMock(return_value=self.overview()), alert_preferences=AsyncMock())
        for callback, change in (('alert_pref:enabled:off', {'enabled': False}),
            ('alert_pref:notify_resolved:on', {'notify_resolved': True})):
            self.query.data = callback
            with patch.object(alerts, 'render', new_callable=AsyncMock):
                await alerts.alerts_cb(self.query, self.bot, backend, self.state)
                await alerts.alerts_cb(self.query, self.bot, backend, self.state)
            backend.alert_preferences.assert_awaited_with(123, change)
        backend.alert_preferences.reset_mock()
        for callback in ('alert_pref:other:on', 'alert_pref:interval_minutes:0', 'alert_pref:enabled:maybe'):
            self.query.data = callback
            with patch.object(alerts, 'render', new_callable=AsyncMock):
                await alerts.alerts_cb(self.query, self.bot, backend, self.state)
        backend.alert_preferences.assert_not_awaited()

    async def test_active_conditions_link_to_node_and_page_clamps_after_recovery(self):
        value = {'active': [dict(node_key=f'n{i}', title=f'Node {i}', flag='🇱🇻',
            kind='xray_down', last_seen_at='2026-10-05T10:00:00+00:00') for i in range(9)]}
        backend = SimpleNamespace(alerts_overview=AsyncMock(return_value=value))
        self.query.data = 'alerts_active:8'
        with patch.object(alerts, 'render', new_callable=AsyncMock) as draw:
            await alerts.alerts_cb(self.query, self.bot, backend, self.state)
        screen, rows = draw.call_args.args[2:4]
        self.assertEqual(screen.sections[0].rows[0][0].callback_data, 'admin_node:n8')
        self.assertTrue(screen.sections[0].sections[0].collapsed)
        self.assertEqual(rows[0][0].text, '←')
        value['active'] = value['active'][:1]
        with patch.object(alerts, 'render', new_callable=AsyncMock) as draw:
            await alerts.alerts_cb(self.query, self.bot, backend, self.state)
        screen, rows = draw.call_args.args[2:4]
        self.assertEqual(screen.sections[0].rows[0][0].callback_data, 'admin_node:n0')
        self.assertEqual(len(rows), 1)
        value['active'] = []
        with patch.object(alerts, 'render', new_callable=AsyncMock) as draw:
            await alerts.alerts_cb(self.query, self.bot, backend, self.state)
        self.assertEqual(draw.call_args.args[2].sections, ())


class AlertNoticeTests(IsolatedAsyncioTestCase):
    async def test_alert_and_recovery_have_only_close_and_explicit_percentages(self):
        from telegram_client import alert_delivery
        for resolved in (False,True):
            event = {'node_key':'lv1','kind':'disk_low','resolved':resolved,'at':'2026-10-14T10:00:00+00:00',
                     'payload':{'node_title':'Latvia #1','value':18 if resolved else 3}}
            delivery = {'id':'notice','telegram_user_id':101,'locale':'en','event':event}
            backend = SimpleNamespace(alert_claim=AsyncMock(return_value={'delivery':delivery}),alert_ack=AsyncMock())
            with patch.object(alert_delivery,'send_notification',new_callable=AsyncMock,return_value='sent') as send:
                await alert_delivery.deliver_one(SimpleNamespace(),backend)
            screen = send.call_args.args[2]
            text = screen.plain()
            self.assertIn('Disk space available: '+('18%' if resolved else '3%'),text)
            self.assertEqual([b.callback_data for row in send.call_args.kwargs['rows'] for b in row],['update_notice_close'])
            self.assertEqual(screen.rich(send.call_args.kwargs['rows']).blocks[-1].type,'buttons')
            self.assertNotIn('Measured value',text)

    async def test_all_resource_notices_show_units_and_recovery_is_not_a_failure(self):
        from telegram_client.alert_delivery import alert_screen
        for locale in ('en','ru'):
            for kind in ('disk_low','ram_high','cpu_high','load_high'):
                for resolved in (False,True):
                    screen = alert_screen({'locale':locale,'event':{'kind':kind,'resolved':resolved,'at':'2026-10-14T10:00:00Z',
                        'payload':{'node_title':'Node','value':3,'cpus':2,'severity':'critical'}}})
                    text = screen.plain()
                    self.assertNotIn('alerts.',text)
                    if kind == 'load_high':
                        self.assertIn('CPU',text)
                        self.assertIn('2',text)
                    else:
                        self.assertIn('3%',text)
                    if resolved:
                        self.assertNotIn('Severity:',text)
                        self.assertNotIn('Уровень:',text)
