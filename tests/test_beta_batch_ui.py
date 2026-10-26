"""Regression tests for beta batch UI updates."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.i18n import tr
from telegram_client.routers import admin_nodes, quick_start
from telegram_client.routers.callbacks import (
    AdminNodeCallback,
    NodeMaintenanceCallback,
    RetireRegistryCallback,
)
from tests import test_telegram_client as fixture


class BetaBatchUITelegramTests(IsolatedAsyncioTestCase):
    setUp = fixture.TelegramFlowTests.setUp

    async def test_uninstalled_registry_confirmation_is_short_with_remove_label(self):
        node = {
            'key': 'lv1',
            'title': 'Latvia VPS',
            'flag': '🇱🇻',
            'region': 'Europe',
            'applied_revision': 0,
            'setup_attempted': False,
        }
        backend = SimpleNamespace(request=AsyncMock(return_value=node))
        for lang in ('en', 'ru'):
            self.state_data['locale'] = lang
            with patch.object(admin_nodes, 'render', new_callable=AsyncMock) as draw:
                await admin_nodes.show_registry_confirmation(
                    123, 123, 77, 'lv1', self.bot, backend, self.state
                )
            screen, rows = draw.call_args.args[2:4]
            # Title is "Remove server"
            self.assertEqual(screen.title, tr(lang, 'nodes.maintenance.remove'))
            # Confirmation text is short: server label + grants warning only
            self.assertEqual(
                screen.lines,
                (
                    admin_nodes.server_label(node),
                    tr(lang, 'nodes.maintenance.registry_grants'),
                ),
            )
            self.assertEqual(len(screen.lines), 2)
            # Must NOT include warnings for runtime, tunnels, or active use
            self.assertNotIn(tr(lang, 'nodes.maintenance.registry_runtime'), screen.lines)
            self.assertNotIn(tr(lang, 'nodes.maintenance.registry_tunnels'), screen.lines)
            self.assertNotIn(tr(lang, 'nodes.maintenance.registry_use'), screen.lines)
            # Danger action uses "Remove" label
            self.assertEqual(rows[0][0].text, tr(lang, 'nodes.maintenance.remove'))
            self.assertEqual(
                rows[0][0].callback_data,
                RetireRegistryCallback(node_key='lv1').pack(),
            )
            # Back button returns to uninstalled node card
            self.assertEqual(
                rows[1][0].callback_data,
                AdminNodeCallback(node_key='lv1').pack(),
            )

    async def test_installed_registry_confirmation_retains_warnings_and_registry_label(self):
        node = {
            'key': 'lv1',
            'title': 'Latvia VPS',
            'flag': '🇱🇻',
            'region': 'Europe',
            'applied_revision': 3,
            'setup_attempted': True,
        }
        backend = SimpleNamespace(request=AsyncMock(return_value=node))
        for lang in ('en', 'ru'):
            self.state_data['locale'] = lang
            with patch.object(admin_nodes, 'render', new_callable=AsyncMock) as draw:
                await admin_nodes.show_registry_confirmation(
                    123, 123, 77, 'lv1', self.bot, backend, self.state
                )
            screen, rows = draw.call_args.args[2:4]
            # Title is "Remove from registry only"
            self.assertEqual(screen.title, tr(lang, 'nodes.maintenance.registry_only'))
            # Retains all warnings for installed servers
            self.assertIn(tr(lang, 'nodes.maintenance.registry_grants'), screen.lines)
            self.assertIn(tr(lang, 'nodes.maintenance.registry_runtime'), screen.lines)
            self.assertIn(tr(lang, 'nodes.maintenance.registry_tunnels'), screen.lines)
            self.assertIn(tr(lang, 'nodes.maintenance.registry_use'), screen.lines)
            self.assertEqual(
                screen.lines,
                (
                    admin_nodes.server_label(node),
                    tr(lang, 'nodes.maintenance.registry_grants'),
                    tr(lang, 'nodes.maintenance.registry_runtime'),
                    tr(lang, 'nodes.maintenance.registry_tunnels'),
                    tr(lang, 'nodes.maintenance.registry_use'),
                ),
            )
            # Danger action uses "Remove from registry" confirm label
            self.assertEqual(
                rows[0][0].text, tr(lang, 'nodes.maintenance.registry_confirm')
            )
            self.assertEqual(
                rows[0][0].callback_data,
                RetireRegistryCallback(node_key='lv1').pack(),
            )
            # Back button returns to node maintenance
            self.assertEqual(
                rows[1][0].callback_data,
                NodeMaintenanceCallback(node_key='lv1').pack(),
            )

    async def test_quick_start_current_step_note_below_progress_without_top_lines(self):
        for step in ('defaults', 'node', 'protocols', 'access', 'config', None):
            backend = SimpleNamespace(
                request=AsyncMock(
                    return_value={
                        'steps': [
                            {'key': 'defaults', 'complete': step != 'defaults'},
                            {'key': 'node', 'complete': False},
                        ],
                        'next_step': step,
                        'node_key': 'lv1' if step == 'protocols' else None,
                        'profile_id': None,
                    }
                )
            )
            for lang in ('en', 'ru'):
                self.state_data['locale'] = lang
                with patch.object(quick_start, 'render', new_callable=AsyncMock) as draw:
                    await quick_start.show(123, 123, 77, self.bot, backend, self.state)
                screen, _ = draw.call_args.args[2:4]
                # Screen.lines must be empty (no top lines)
                self.assertEqual(screen.lines, ())
                # Two sections: 0 is progress, 1 is the current step note
                self.assertEqual(len(screen.sections), 2)
                self.assertEqual(
                    screen.sections[0].title, tr(lang, 'quick_start.progress')
                )
                expected_note = tr(lang, 'quick_start.note.' + (step or 'done'))
                self.assertEqual(screen.sections[1].lines, (expected_note,))
