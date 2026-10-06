"""Maintenance screens preserve safety and navigation through Rich and fallback UI."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.routers import admin_backups as backups, admin_system_cleanup as cleanup
from telegram_client.routers import admin_settings as settings
from telegram_client.i18n import CATALOG, tr
from telegram_client.backend import BackendError
from tests import test_telegram_client as fixture


class MaintenanceRichTests(IsolatedAsyncioTestCase):
    setUp = fixture.TelegramFlowTests.setUp

    async def test_restore_admission_failure_shows_specific_reason_without_freeze_warning(self):
        for lang in ('en', 'ru'):
            self.state_data.update(locale=lang, backup_draft={
                'nonce': 'test', 'key': 'command', 'body': {'action': 'restore'}})
            self.query.data = 'backup_submit:test'
            for code in ('maintenance_busy', 'backup_incompatible', 'backup_pending'):
                backend = SimpleNamespace(backup_command=AsyncMock(
                    side_effect=BackendError(code, 409)))
                with patch.object(backups, 'render', AsyncMock()) as draw:
                    await backups.backup_cb(self.query, self.bot, backend, self.state)
                screen, rows = draw.call_args.args[2:4]
                self.assertIn(tr(lang, 'backups.error.' + code), screen.plain())
                self.assertNotIn(tr(lang, 'backups.failed_note'), screen.plain())
                self.assertEqual(rows[-1][0].callback_data, 'backups')
                screen.rich(rows)

    async def test_restore_revocation_failure_keeps_specific_freeze_warning(self):
        backend = SimpleNamespace(backup_job=AsyncMock(return_value={
            'status': 'blocked', 'action': 'restore', 'phase': 'revoking',
            'result': {'status': 'failed', 'code': 'backup_revocations_failed'}}))
        for lang in ('en', 'ru'):
            self.state_data['locale'] = lang
            with patch.object(backups, 'render', AsyncMock()) as draw:
                await backups.result(self.query, self.bot, backend, self.state, 'job')
            screen, rows = draw.call_args.args[2:4]
            self.assertIn(tr(lang, 'backups.error.backup_revocations_failed'), screen.plain())
            screen.rich(rows)

    def value(self):
        return dict(count=3, size_bytes=1048576, latest={'created_at': '2026-10-05T10:00:00'},
            enabled=True, interval_hours=24, keep_count=10, last_job=None)

    async def test_backup_summary_and_settings_render_in_both_locales(self):
        backend = SimpleNamespace(backups_overview=AsyncMock(return_value=self.value()))
        for lang in ('en', 'ru'):
            self.state_data['locale'] = lang
            with patch.object(backups, 'render', AsyncMock()) as draw:
                await backups.overview(self.query, self.bot, backend, self.state)
            screen, rows = draw.call_args.args[2:4]
            self.assertTrue(screen.embedded_buttons)
            self.assertEqual(len(screen.sections[0].tables[0].rows), 3)
            self.assertTrue(screen.sections[-1].collapsed)
            self.assertIn('backup_create', [b.callback_data for row in screen.fallback_rows(rows) for b in row])
            self.assertNotIn('backups.rich.', screen.plain())
            screen.rich(rows)
            with patch.object(backups, 'render', AsyncMock()) as draw:
                await backups.settings(self.query, self.bot, backend, self.state)
            screen, rows = draw.call_args.args[2:4]
            self.assertEqual(screen.sections[0].rows[0][0].style, 'primary')
            self.assertEqual([b.callback_data for b in screen.sections[0].rows[0]],
                ['backup_pref:enabled:1', 'backup_pref:enabled:0'])
            self.assertNotIn('backups.rich.', screen.plain())
            screen.rich(rows)

    async def test_small_backup_sizes_are_nonzero_in_rich_and_plain_views(self):
        for lang in ('en', 'ru'):
            self.state_data['locale'] = lang
            for size, label in ((0, '0 B'), (900, '900 B'), (8192, '8.0 KiB'),
                    (1048576, '1.0 MiB'), (1073741824, '1.0 GiB')):
                backend = SimpleNamespace(backups_overview=AsyncMock(return_value={**self.value(), 'size_bytes': size}))
                with patch.object(backups, 'render', AsyncMock()) as draw:
                    await backups.overview(self.query, self.bot, backend, self.state)
                screen, rows = draw.call_args.args[2:4]
                self.assertEqual(screen.sections[0].tables[0].rows[1][1], label)
                self.assertIn(label, screen.plain())
                screen.rich(rows)

    async def test_backup_preferences_set_explicit_value_and_reject_invalid_callbacks(self):
        backend = SimpleNamespace(backups_overview=AsyncMock(return_value=self.value()),
            backup_preferences=AsyncMock())
        self.query.data = 'backup_pref:enabled:1'
        with patch.object(backups, 'render', AsyncMock()):
            for _ in range(2):
                await backups.backup_cb(self.query, self.bot, backend, self.state)
        self.assertEqual(backend.backup_preferences.await_count, 2)
        self.assertTrue(all(call.args[1] == {'enabled': True} for call in backend.backup_preferences.call_args_list))
        backend.backup_preferences.reset_mock()
        for callback in ('backup_pref:keep_count:0', 'backup_pref:unknown:1', 'backup_pref:enabled:2'):
            self.query.data = callback
            with patch.object(backups, 'render', AsyncMock()):
                await backups.backup_cb(self.query, self.bot, backend, self.state)
        backend.backup_preferences.assert_not_awaited()

    async def test_restore_metadata_is_visible_and_confirmation_is_danger_styled(self):
        backend = SimpleNamespace(backup_detail=AsyncMock(return_value=dict(id='snapshot',
            created_at='2026-10-05T10:00:00', app_version='0.4.3', profiles=2, nodes=3,
            compatible=True, checksum='checksum')))
        self.query.data = 'backup_detail:snapshot'
        with patch.object(backups, 'render', AsyncMock()) as draw:
            await backups.backup_cb(self.query, self.bot, backend, self.state)
        screen, rows = draw.call_args.args[2:4]
        self.assertEqual(len(screen.sections[0].tables[0].rows), 4)
        self.assertEqual(rows[0][0].style, 'danger')
        self.query.data = 'backup_restore:snapshot'
        with patch.object(backups, 'render', AsyncMock()) as draw:
            await backups.backup_cb(self.query, self.bot, backend, self.state)
        screen, rows = draw.call_args.args[2:4]
        self.assertEqual(rows[0][1].style, 'danger')
        self.assertIn(tr('en', 'backups.restore_warning'), screen.plain())
        self.assertEqual(screen.rich(rows).blocks[-1].buttons[1].style, 'danger')

    async def test_backup_catalog_retains_arrows_and_page_context(self):
        backend = SimpleNamespace(backup_catalog=AsyncMock(return_value=dict(
            items=[dict(id='snapshot', created_at='2026-10-05T10:00:00', app_version='0.4.3')],
            next_offset=16, total=20)))
        with patch.object(backups, 'render', AsyncMock()) as draw:
            await backups.catalog(self.query, self.bot, backend, self.state, 8)
        screen, rows = draw.call_args.args[2:4]
        self.assertEqual([b.text for b in rows[-2]], ['←', '→'])
        self.assertIn(tr('en', 'pagination.page', page=2, pages=3), screen.lines)
        self.assertEqual(self.state_data['backup_offset'], 8)
        screen.rich(rows)

    async def test_release_cleanup_inventory_keeps_visible_result_and_red_action(self):
        backend = SimpleNamespace(cleanup_overview=AsyncMock(return_value=dict(supported=True,
            install_mode='simple', current_target='/opt/node-plane/releases/current',
            total_releases=5, kept_releases=2, removable_releases=3, removable_size_bytes=1048576)))
        with patch.object(settings, 'render', AsyncMock()) as draw:
            await settings.show_release_cleanup(self.query, self.bot, backend, self.state, result_status='success')
        screen, rows = draw.call_args.args[2:4]
        self.assertEqual(len(screen.sections[0].tables[0].rows), 4)
        self.assertEqual(rows[0][0].style, 'danger')
        self.assertTrue(screen.sections[-1].collapsed)
        self.assertTrue(screen.lines)
        self.assertNotIn('/opt/node-plane', str(screen.lines))
        screen.rich(rows)

    async def test_cleanup_root_separates_reset_and_remove_and_preserves_all_actions(self):
        backend = SimpleNamespace(system_cleanup_overview=AsyncMock(return_value=dict(
            supported=True, counts=dict(accounts=2, profiles=2, nodes=3), latest_job=None)))
        for lang in ('en', 'ru'):
            self.state_data['locale'] = lang
            with patch.object(cleanup, 'render', AsyncMock()) as draw:
                await cleanup.show_root(123, 123, 77, self.bot, backend, self.state)
            screen, rows = draw.call_args.args[2:4]
            self.assertEqual(len(screen.sections[0].tables[0].rows), 3)
            callbacks = {b.callback_data for row in screen.fallback_rows(rows) for b in row}
            self.assertTrue({'sc_plan:reset:0', 'sc_plan:reset:1', 'sc_plan:remove:0',
                'sc_plan:remove:1', 'admin_settings'} <= callbacks)
            self.assertTrue(all(b.style == 'danger' for section in screen.sections[1:] for row in section.rows for b in row))
            self.assertNotIn('system_cleanup.rich.', screen.plain())
            screen.rich(rows)

    async def test_cleanup_job_keeps_recovery_controls_and_collapses_technical_details(self):
        backend = SimpleNamespace(system_cleanup_job=AsyncMock(return_value=dict(
            status='blocked', phase='nodes', backup_id='private-backup-id', error_code='node_cleanup_unverified',
            items=[dict(node_key='lv1', status='blocked')])))
        with patch.object(cleanup, 'render', AsyncMock()) as draw:
            await cleanup.show_job(123, 123, 77, 'job', self.bot, backend, self.state)
        screen, rows = draw.call_args.args[2:4]
        self.assertTrue(screen.sections[-1].collapsed)
        self.assertNotIn('private-backup-id', str(screen.lines))
        self.assertTrue({'sc_retry:job', 'sc_abort:job'} <= {b.callback_data for row in rows for b in row})
        screen.rich(rows)

    async def test_all_router_screens_embed_navigation_and_new_labels_have_both_locales(self):
        root = Path(__file__).resolve().parents[1] / 'app/telegram_client/routers'
        for path in root.glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'Screen':
                    keywords = {k.arg: k.value for k in node.keywords}
                    self.assertIn('embedded_buttons', keywords, f'{path.name}:{node.lineno}')
        prefixes = ('maintenance.rich.', 'backups.rich.', 'cleanup.rich.', 'command.rich.', 'system_cleanup.rich.')
        self.assertEqual({k for k in CATALOG['en'] if k.startswith(prefixes)},
            {k for k in CATALOG['ru'] if k.startswith(prefixes)})
