"""Core controller settings retain their actions in Rich and fallback views."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.routers import admin_settings as settings
from telegram_client.backend import BackendError
from tests import test_telegram_client as fixture


class AdminSettingsRichTests(IsolatedAsyncioTestCase):
    setUp = fixture.TelegramFlowTests.setUp

    async def test_hub_groups_actions_and_collapses_destructive_actions(self):
        for locale in ('en', 'ru'):
            self.state_data['locale'] = locale
            with patch.object(settings, 'render', new_callable=AsyncMock) as draw:
                await settings.admin_settings_cb(self.query, self.bot, SimpleNamespace(), self.state)
            screen, rows = draw.call_args.args[2:4]
            self.assertEqual(len(screen.sections), 4)
            self.assertTrue(screen.sections[-1].collapsed)
            recovery_rows = [row for section in screen.sections for row in section.rows
                if any(b.callback_data == settings.RecoveryPage().pack() for b in row)]
            self.assertEqual([len(row) for row in recovery_rows], [1])
            callbacks = [b.callback_data for row in screen.fallback_rows(rows) for b in row]
            for value in ('bot_title_settings', 'backups', 'alerts', 'traffic', 'system_cleanup', 'admin_menu'):
                self.assertIn(value, callbacks)
            self.assertEqual([b.type for b in screen.rich(rows).blocks[-2:]], ['divider', 'buttons'])

    async def test_request_choices_highlight_saved_values_and_set_explicit_values(self):
        for locale in ('en', 'ru'):
            self.state_data['locale'] = locale
            for enabled in (True, False):
                policy = {'enabled': enabled, 'notify_requests': not enabled, 'gate_message': 'Welcome'}
                backend = SimpleNamespace(access_request_policy=AsyncMock(return_value=policy),
                    update_access_request_policy=AsyncMock())
                with patch.object(settings, 'render', new_callable=AsyncMock) as draw:
                    await settings.show_request_policy(123, 123, 77, self.bot, backend, self.state)
                screen, rows = draw.call_args.args[2:4]
                self.assertEqual([b.style for b in screen.sections[0].rows[0]],
                                 ['primary', None] if enabled else [None, 'primary'])
                self.assertEqual([b.style for b in screen.sections[1].rows[0]],
                                 [None, 'primary'] if enabled else ['primary', None])
                self.assertEqual(screen.sections[2].lines, ('Welcome',))
                screen.rich(rows)
                for callback, field, selected in (('request_policy_enabled:on', 'enabled', True),
                    ('request_policy_notify:off', 'notify_requests', False)):
                    self.query.data = callback
                    with patch.object(settings, 'show_request_policy', new_callable=AsyncMock):
                        await settings.request_policy_choice_cb(self.query, self.bot, backend, self.state)
                        await settings.request_policy_choice_cb(self.query, self.bot, backend, self.state)
                    backend.update_access_request_policy.assert_awaited_with(123, {field: selected})

    async def test_ssh_public_key_is_copyable_and_downloadable_without_private_key(self):
        public = 'ssh-ed25519 dGVzdA== node-plane'
        backend = SimpleNamespace(request=AsyncMock(return_value={'public_key': public}))
        for locale in ('en', 'ru'):
            self.state_data['locale'] = locale
            with patch.object(settings, 'render', new_callable=AsyncMock) as draw:
                await settings.ssh_key_cb(self.query, self.bot, backend, self.state)
            screen, rows = draw.call_args.args[2:4]
            self.assertEqual(screen.uri, public)
            self.assertEqual(screen.files, (('node-plane.pub', (public + '\n').encode()),))
            self.assertTrue(screen.sections[0].lines[0].startswith('SHA256:'))
            self.assertEqual(screen.plain_entities()[0].type, 'code')
            screen.rich(rows)

    async def test_unavailable_traffic_policy_keeps_back_and_does_not_show_choices(self):
        self.query.data = 'traffic'
        backend = SimpleNamespace(traffic_policy=AsyncMock(side_effect=BackendError('backend_unavailable', 503)))
        with patch.object(settings, 'render', new_callable=AsyncMock) as draw:
            await settings.traffic_settings_cb(self.query, self.bot, backend, self.state)
        screen, rows = draw.call_args.args[2:4]
        self.assertEqual(screen.sections, ())
        self.assertTrue(screen.embedded_buttons)
        self.assertEqual(len(rows), 1)

    async def test_auto_check_opens_settings_without_toggling_and_highlights_values(self):
        backend = SimpleNamespace(updates_overview=AsyncMock(return_value={
            'auto_check_enabled':True, 'auto_check_interval_minutes':360}),
            update_preferences=AsyncMock())
        for locale in ('en', 'ru'):
            self.state_data['locale'] = locale
            self.query.data = 'upd_act:auto_check'
            with patch.object(settings, 'render', new_callable=AsyncMock) as draw:
                await settings.update_action_cb(self.query, settings.UpdateActionCallback.unpack(self.query.data), self.bot, backend, self.state)
            backend.update_preferences.assert_not_called()
            screen, rows = draw.call_args.args[2:4]
            self.assertEqual(screen.sections[0].rows[0][0].style, 'primary')
            choices = [b for row in screen.sections[1].rows for b in row]
            self.assertEqual([b.callback_data for b in choices if b.style == 'primary'], ['upd_act:auto_interval_360'])
            self.assertEqual(rows[-1][0].callback_data, 'updates')
            screen.rich(rows)
        self.query.data = 'upd_act:auto_interval_15'
        with patch.object(settings, 'show_auto_check', new_callable=AsyncMock):
            await settings.update_action_cb(self.query, settings.UpdateActionCallback.unpack(self.query.data), self.bot, backend, self.state)
        backend.update_preferences.assert_awaited_once_with(123, {'auto_check_interval_minutes':15})

    async def test_update_notification_close_preserves_control_panel(self):
        self.state_data['control_message_id'] = 999
        before = dict(self.state_data)
        self.query.message.delete = AsyncMock()
        await settings.update_notice_close(self.query)
        self.query.message.delete.assert_awaited_once()
        self.assertEqual(self.state_data, before)
