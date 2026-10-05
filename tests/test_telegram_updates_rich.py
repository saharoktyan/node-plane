"""Compact stack update UI preserves advanced operations and region paging."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.routers import admin_updates as updates
from tests import test_telegram_client as fixture


class UpdatesRichTests(IsolatedAsyncioTestCase):
    setUp = fixture.TelegramFlowTests.setUp

    async def test_main_updates_keeps_advanced_actions_collapsed_and_paginates_servers(self):
        fleet = {'nodes': [dict(key=f'n{i:02}', title=f'Node {i:02}', region='Europe', flag='🇱🇻',
            agent_status='required', runtime_status='current') for i in range(11)],
            'driver_status': 'current', 'agents_required': True}
        overview = {'update_supported': True, 'update_available': True, 'current_version': 'old',
            'remote_label': 'new', 'auto_check_enabled': True}
        backend = SimpleNamespace(updates_overview=AsyncMock(return_value=overview),
            update_rollout=AsyncMock(return_value=fleet))
        for lang in ('en', 'ru'):
            self.state_data['locale'] = lang
            with patch.object(updates, 'render', new_callable=AsyncMock) as draw:
                await updates.show_overview(self.query, self.bot, backend, self.state)
            screen, rows = draw.call_args.args[2:4]
            self.assertEqual(len(screen.sections[0].tables[0].rows), 4)
            self.assertTrue(screen.sections[2].collapsed)
            self.assertEqual(len(screen.sections[2].sections[0].sections), 10)
            self.assertTrue(screen.sections[-1].collapsed)
            callbacks = [b.callback_data for row in screen.fallback_rows(rows) for b in row]
            for action in ('ufleet', 'uv_page:0', 'upd_act:branch_menu', 'upd_act:cleanup_menu', 'upd_act:run'):
                self.assertIn(action, callbacks)
            self.assertEqual([b.size for b in screen.rich(rows).blocks if b.type == 'heading'][0], 1)
            with patch.object(updates, 'render', new_callable=AsyncMock) as draw:
                await updates.show_overview(self.query, self.bot, backend, self.state, 1, opened=True)
            servers = draw.call_args.args[2].sections[2]
            self.assertTrue(servers.is_open)
            self.assertEqual(len(servers.sections[0].sections), 1)
            self.assertEqual(servers.rows[0][0].text, '←')
            self.assertEqual(servers.sections[0].sections[0].heading_size, 3)

    async def test_latest_action_queues_whole_stack(self):
        backend = SimpleNamespace(updates_overview=AsyncMock(return_value={'update_available': True, 'remote_version': '0.4.3-alpha.36'}),
            update_versions=AsyncMock(return_value={'branch': 'dev', 'items': [dict(allowed=True,
                ref='v0.4.3-alpha.36', version='0.4.3-alpha.36')]}))
        with patch.object(updates, 'render', new_callable=AsyncMock):
            await updates.confirm_latest(self.query, self.bot, backend, self.state)
        self.assertEqual(self.state_data['update_draft']['body'],
            {'kind': 'stack', 'target_ref': 'v0.4.3-alpha.36', 'branch': 'dev'})

    async def test_returning_to_updates_restores_persisted_progress_button(self):
        job = dict(id='persisted-job', kind='stack', status='running', items=[], result={})
        backend = SimpleNamespace(updates_overview=AsyncMock(return_value={
            'update_supported': True, 'update_available': True, 'latest_job': job}),
            update_rollout=AsyncMock(return_value={'nodes': [], 'driver_status': 'current'}))
        self.state_data.clear()
        with patch.object(updates, 'render', new_callable=AsyncMock) as draw:
            await updates.show_overview(self.query, self.bot, backend, self.state)
        screen, rows = draw.call_args.args[2:4]
        callbacks = [b.callback_data for row in screen.fallback_rows(rows) for b in row]
        self.assertIn('update_job:persisted-job', callbacks)
        self.assertNotIn('upd_act:run', callbacks)

    async def test_partial_result_has_clear_failures_and_preserves_component_progress(self):
        job = dict(id='j1', kind='stack', status='partial', target_ref='new', items=[
            dict(node_key='lv1', title='Latvia', flag='🇱🇻', region='Europe', status='blocked', error_code='node_update_failed')],
            result={'components': {k: 'succeeded' for k in ('backend', 'worker', 'driver', 'telegram')}})
        backend = SimpleNamespace(update_job=AsyncMock(return_value=job))
        with patch.object(updates, 'render', new_callable=AsyncMock) as draw:
            await updates.show_job(self.query, self.bot, backend, self.state, 'j1')
        screen, rows = draw.call_args.args[2:4]
        self.assertEqual(len(screen.sections[0].tables[0].rows), 4)
        self.assertEqual(screen.sections[1].rows[0][0].callback_data, 'admin_node:lv1')
        self.assertIn(updates.tr('en', 'updates.rich.partial_note'), screen.lines)
        self.assertTrue(screen.sections[-1].collapsed)
        screen.rich(rows)
