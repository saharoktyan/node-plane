from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.routers import user, admin_settings, admin_backups
from telegram_client.navigation import parent_path
from telegram_client.backend import BackendError
from tests import test_telegram_client as fixture


class AdminNavigationSectionsTests(IsolatedAsyncioTestCase):
    setUp = fixture.TelegramFlowTests.setUp

    async def test_update_attention_highlights_updates_and_keeps_settings_plain(self):
        overview = {'pending_requests': 0, 'nodes_enabled': 1, 'nodes_total': 1,
            'profiles_active': 1, 'profiles_total': 1, 'problem_nodes': [], 'updates': {'available': True}}
        backend = SimpleNamespace(bot_title=AsyncMock(return_value={'title': 'Node Plane'}),
            request=AsyncMock(return_value={'completed': True}), admin_overview=AsyncMock(return_value=overview))
        for locale in ('ru', 'en'):
            self.state_data['locale'] = locale
            with patch.object(user, 'render', new_callable=AsyncMock) as draw:
                await user.show_admin_menu(123, 123, 77, self.bot, backend, self.state)
            screen, rows = draw.call_args.args[2:4]
            buttons = {b.callback_data: b for row in screen.fallback_rows(rows) for b in row}
            self.assertEqual(buttons['updates'].style, 'primary')
            self.assertIsNone(buttons['admin_settings'].style)
            self.assertIsNone(buttons['backups'].style)
            self.assertEqual(rows[-1][0].callback_data.startswith('u:'), True)

    def test_stale_update_flag_does_not_highlight_current_or_older_release(self):
        from backend.admin_overview import known_update_available
        with patch('backend.admin_overview.APP_SEMVER', '1.0.0-alpha.4'):
            for remote, available in [('1.0.0-alpha.3',False), ('1.0.0-alpha.4',False), ('1.0.0-alpha.5',True), ('',False)]:
                self.assertEqual(known_update_available({'updates_update_available':'1','updates_remote_version':remote}), available)

    async def test_updates_overview_failure_returns_to_admin_panel(self):
        backend = SimpleNamespace()
        for locale in ('ru', 'en'):
            self.state_data['locale'] = locale
            with patch.object(admin_settings, 'show_updates', new_callable=AsyncMock,
                    side_effect=BackendError('unavailable', 503)), patch.object(
                    admin_settings, 'render', new_callable=AsyncMock) as draw:
                await admin_settings.updates_menu_cb(self.query, self.bot, backend, self.state)
            self.assertEqual(draw.call_args.args[3][-1][0].callback_data, 'admin_menu')

    async def test_backup_detail_and_confirmation_keep_catalog_level_and_offset(self):
        self.state_data['backup_offset'] = 8
        for locale in ('ru', 'en'):
            path = parent_path('backup_detail:id', locale, self.state_data)
            self.assertEqual([p.callback for p in path],
                ['admin_menu', 'backups', 'backup_list:8', 'backup_detail:id'])
            for callback in ('updates', 'upd_act:auto_check', 'uv_page:0', 'update_job:id',
                             'backups', 'backup_settings', 'backup_list:8', 'backup_job:id'):
                values = [p.callback for p in parent_path(callback, locale, self.state_data)]
                self.assertEqual(values[0], 'admin_menu')
                self.assertNotIn('admin_settings', values)

    async def test_uninstalled_node_draft_controls_empty_in_submenus_and_setup_on_root_only(self):
        from telegram_client.routers import admin_nodes
        self.state_data['locale'] = 'en'
        uninstalled_node = {
            'key': 'n1', 'title': 'Node 1', 'region': 'Europe', 'transport': 'ssh',
            'ssh_target': 'root@n1.example', 'protocols': ['awg'], 'settings': {'public_host': 'n1.example'},
            'applied_revision': 0, 'desired_revision': 1, 'enabled': False
        }
        # In settings submenus (no unapplied draft), draft_controls returns []
        controls = await admin_nodes.draft_controls(uninstalled_node, self.state, 'en')
        self.assertEqual(controls, [])

        # On root (show_admin_node), bootstrap/setup button is offered in installation section
        backend = SimpleNamespace(
            node=AsyncMock(return_value=uninstalled_node),
            node_overview=AsyncMock(return_value={'ready': 0, 'pending': 0, 'failed': 0, 'attention': 0}),
            admin_overview=AsyncMock(return_value={}),
            request=AsyncMock(return_value=uninstalled_node)
        )
        with patch.object(admin_nodes, 'render', new_callable=AsyncMock) as draw:
            await admin_nodes.show_admin_node(123, 123, 77, 'n1', self.bot, backend, self.state)
        screen = draw.call_args.args[2]
        install_section = next(s for s in screen.sections if s.title == 'Installation and checks')
        callbacks = [b.callback_data for row in install_section.rows for b in row]
        self.assertTrue(any(c == 'bootstrap_menu:n1' for c in callbacks))

    async def test_probe_shows_disk_usage_percentage_and_used_over_total_bytes(self):
        from telegram_client.routers import admin_nodes
        self.state_data['locale'] = 'en'
        node_data = {
            'key': 'n1', 'title': 'Node 1', 'region': 'Europe', 'transport': 'ssh',
            'ssh_target': 'root@n1.example', 'protocols': ['awg'], 'settings': {'public_host': 'n1.example'},
            'applied_revision': 1, 'desired_revision': 1, 'enabled': True
        }
        observation = {
            'applied_revision': 1, 'desired_revision': 1,
            'protocols': [{'protocol': 'awg', 'installed': True, 'running': True, 'config_valid': True,
                'ports': [{'transport': 'udp', 'network': 'udp', 'port': 51820}]}],
            'docker': True,
            'checked_at': '2026-10-25T12:00:00+00:00',
            'response_ms': 42,
            'host_metrics': {
                'cpu_used_percent': 12.5,
                'disk_free_percent': 25.0,  # 25% free -> 75% used
                'disk_total_bytes': 100 * 1024 * 1024 * 1024,
                'disk_free_bytes': 25 * 1024 * 1024 * 1024,  # used = 75 GB
            }
        }
        backend = SimpleNamespace(
            request=AsyncMock(return_value=node_data),
            node_probe=AsyncMock(return_value=observation)
        )
        self.query.data = 'probe_node:n1'
        with patch.object(admin_nodes, 'render', new_callable=AsyncMock) as draw:
            await admin_nodes.probe_node_cb(self.query, admin_nodes.ProbeNodeCallback(node_key='n1'), self.bot, backend, self.state)
        screen = draw.call_args.args[2]
        res_section = next(s for s in screen.sections if s.title == 'Host resources')
        table = res_section.tables[0]
        row_dict = dict(table.rows)
        self.assertIn('Disk usage', row_dict)
        self.assertEqual(row_dict['Disk usage'], '75% · 75.0 GiB / 100.0 GiB')
