from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from tests import test_telegram_client as fixture
from telegram_client.routers import admin_settings, admin_alerts
from telegram_client import alert_delivery


class AlertAttentionTests(IsolatedAsyncioTestCase):
    setUp = fixture.TelegramFlowTests.setUp

    async def test_settings_buttons_use_current_backend_attention(self):
        backend = SimpleNamespace(system_attention=AsyncMock(return_value={
            'update_available':True, 'unacknowledged_alerts':1}))
        with patch.object(admin_settings, 'render', new_callable=AsyncMock) as draw:
            await admin_settings.admin_settings_cb(self.query, self.bot, backend, self.state)
        screen, rows = draw.await_args.args[2:4]
        buttons = {b.callback_data:b for row in screen.fallback_rows(rows) for b in row}
        self.assertEqual(buttons['alerts'].style, 'danger')
        self.assertEqual(next(b.style for b in buttons.values() if b.callback_data.startswith('updates')), 'primary')

    async def test_dismiss_reads_new_state_and_keeps_alert_visible(self):
        alert = dict(title='Latvia', node_key='lv1', kind='awg_down', event_id='event')
        backend = SimpleNamespace(alerts_overview=AsyncMock(side_effect=[{'active':[alert]},
            {'active':[{**alert, 'dismissed':True}]}]), dismiss_alert=AsyncMock())
        self.query.data = 'alert_dismiss:event'
        with patch.object(admin_alerts, 'render', new_callable=AsyncMock) as draw:
            await admin_alerts.alerts_cb(self.query, self.bot, backend, self.state)
        backend.dismiss_alert.assert_awaited_once_with(self.query.from_user.id, 'event')
        screen, rows = draw.await_args.args[2:4]
        self.assertIn('Latvia', screen.plain())
        self.assertFalse(any(b.callback_data.startswith('alert_dismiss:')
            for row in screen.fallback_rows(rows) for b in row))

    async def test_preview_sends_only_to_requesting_admin_and_does_not_create_events(self):
        backend = SimpleNamespace(alerts_overview=AsyncMock(return_value=dict(
            active=[], active_count=0, enabled=True, notify_resolved=True,
            interval_minutes=5, delivery_counts={}, last_scan=None)))
        self.query.data = 'alerts_preview_notice'
        with patch('telegram_client.notification_transport.send_notification', new_callable=AsyncMock) as send, \
             patch.object(admin_alerts, 'render', new_callable=AsyncMock):
            await admin_alerts.alerts_cb(self.query, self.bot, backend, self.state)
        self.assertEqual(send.await_args.args[1], self.query.from_user.id)
        self.assertIn('Example', send.await_args.args[2].plain())
