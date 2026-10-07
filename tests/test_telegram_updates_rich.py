"""Compact stack update UI preserves advanced operations and region paging."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.routers import admin_updates as updates
from tests import test_telegram_client as fixture


class UpdatesRichTests(IsolatedAsyncioTestCase):
    setUp = fixture.TelegramFlowTests.setUp

    async def test_update_recovery_actions_match_state_in_both_languages(self):
        for lang in ('en', 'ru'):
            self.state_data['locale'] = lang
            for status, expected in (('awaiting_executor', 'update_cancel:j1'),
                                     ('blocked', 'update_recheck:j1'), ('running', None)):
                backend = SimpleNamespace(update_job=AsyncMock(return_value=dict(
                    id='j1', kind='stack', status=status, items=[], result={})))
                with patch.object(updates, 'render', new_callable=AsyncMock) as draw:
                    await updates.show_job(self.query, self.bot, backend, self.state, 'j1')
                screen, rows = draw.call_args.args[2:4]
                callbacks = [b.callback_data for row in screen.fallback_rows(rows) for b in row]
                if expected:
                    self.assertIn(expected, callbacks)
                else:
                    self.assertNotIn('update_cancel:j1', callbacks)
                self.assertTrue(screen.rich(rows).blocks)

    async def test_update_cancel_requires_confirmation_and_calls_only_cancel_endpoint(self):
        for lang in ('en', 'ru'):
            self.state_data['locale'] = lang
            self.query.data = 'update_cancel:j1'
            backend = SimpleNamespace(request=AsyncMock())
            with patch.object(updates, 'render', new_callable=AsyncMock) as draw:
                await updates.update_tools_cb(self.query, self.bot, backend, self.state)
            rows = draw.call_args.args[3]
            self.assertEqual(rows[0][1].style, 'danger')
            self.assertEqual(rows[0][1].callback_data, 'update_cancel_do:j1')
            backend.request.assert_not_called()
            self.query.data = 'update_cancel_do:j1'
            with patch.object(updates, 'show_job', new_callable=AsyncMock):
                await updates.update_tools_cb(self.query, self.bot, backend, self.state)
            backend.request.assert_awaited_once_with('POST', '/api/v1/system/updates/jobs/j1/cancel',
                telegram_user_id=self.query.from_user.id)

    async def test_controller_only_progress_has_no_empty_server_details(self):
        for lang in ('en', 'ru'):
            self.state_data['locale'] = lang
            job = dict(id='j1', kind='stack', status='succeeded', items=[], result={})
            backend = SimpleNamespace(update_job=AsyncMock(return_value=job))
            with patch.object(updates, 'render', new_callable=AsyncMock) as draw:
                await updates.show_job(self.query, self.bot, backend, self.state, 'j1')
            screen, rows = draw.call_args.args[2:4]
            self.assertFalse(any(block.type == 'details' for block in screen.rich(rows).blocks))
            self.assertTrue(screen.embedded_buttons)
            self.assertEqual(rows[-1][0].callback_data, 'updates')


    async def test_main_updates_groups_visible_actions_and_paginates_outdated_agents(self):
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
            self.assertTrue(all(not section.collapsed for section in screen.sections[3:]))
            self.assertIn(updates.tr(lang, 'updates.current', value='old'), screen.lines)
            self.assertIn(updates.tr(lang, 'updates.available', value='new'), screen.lines)
            callbacks = [b.callback_data for row in screen.fallback_rows(rows) for b in row]
            for action in ('ufleet', 'uv_page:0', 'upd_act:branch_menu', 'upd_act:cleanup_menu', 'upd_act:run', 'upd_act:check'):
                self.assertIn(action, callbacks)
            self.assertEqual(screen.sections[-1].rows[0][0].style, 'danger')
            self.assertIn('upd_act:check', [b.callback_data for b in screen.sections[1].rows[0]])
            self.assertEqual([b.size for b in screen.rich(rows).blocks if b.type == 'heading'][0], 1)
            with patch.object(updates, 'render', new_callable=AsyncMock) as draw:
                await updates.show_overview(self.query, self.bot, backend, self.state, 1, opened=True)
            servers = draw.call_args.args[2].sections[2]
            self.assertTrue(servers.is_open)
            self.assertEqual(len(servers.sections[0].sections), 1)
            self.assertEqual(servers.rows[0][0].text, '←')
            self.assertEqual(servers.sections[0].sections[0].heading_size, 3)

    async def test_outdated_list_excludes_current_and_unknown_agents_and_old_job_nodes(self):
        fleet = {'nodes': [dict(key=key, title=key, agent_status=status) for key, status in
            [('old', 'required'), ('new', 'current'), ('offline', 'unknown')]], 'driver_status': 'current'}
        overview = {'current_version': '0.4.3', 'latest_job': dict(kind='stack', id='previous',
            status='succeeded', result={}, items=[dict(node_key='removed', status='succeeded')])}
        backend = SimpleNamespace(updates_overview=AsyncMock(return_value=overview),
            update_rollout=AsyncMock(return_value=fleet))
        with patch.object(updates, 'render', new_callable=AsyncMock) as draw:
            await updates.show_overview(self.query, self.bot, backend, self.state)
        screen = draw.call_args.args[2]
        servers = screen.sections[2]
        self.assertEqual(servers.title, updates.tr('en', 'updates.rich.outdated_servers'))
        self.assertEqual(len(servers.sections[0].sections), 1)
        self.assertEqual(servers.sections[0].sections[0].rows[0][0].callback_data, 'admin_node:old')
        self.assertEqual(len(screen.lines), 1)
        self.assertEqual(updates.state_label('en', 'current'), 'Up to date')

    async def test_latest_action_queues_whole_stack(self):
        backend = SimpleNamespace(updates_overview=AsyncMock(return_value={'update_available': True, 'remote_version': '0.4.3-alpha.36'}),
            update_versions=AsyncMock(return_value={'branch': 'dev', 'items': [dict(allowed=True,
                ref='v0.4.3-alpha.36', version='0.4.3-alpha.36')]}))
        with patch.object(updates, 'render', new_callable=AsyncMock):
            await updates.confirm_latest(self.query, self.bot, backend, self.state)
        self.assertEqual(self.state_data['update_draft']['body'],
            {'kind': 'stack', 'target_ref': 'v0.4.3-alpha.36', 'branch': 'dev'})

    async def test_latest_action_hidden_without_new_stack_version_even_with_outdated_agents(self):
        backend = SimpleNamespace(updates_overview=AsyncMock(return_value={
            'update_supported': True, 'update_available': False, 'current_version': '0.4.3'}),
            update_rollout=AsyncMock(return_value={'nodes': [], 'driver_status': 'required',
                'agents_required': True, 'runtimes_required': True}))
        with patch.object(updates, 'render', new_callable=AsyncMock) as draw:
            await updates.show_overview(self.query, self.bot, backend, self.state)
        screen, rows = draw.call_args.args[2:4]
        callbacks = [b.callback_data for row in screen.fallback_rows(rows) for b in row]
        self.assertNotIn('upd_act:run', callbacks)
        self.assertIn('upd_act:check', callbacks)
        self.assertIn('ufleet', callbacks)
        self.assertNotIn(updates.tr('en', 'updates.rich.outdated_servers'),
            [section.title for section in screen.sections])

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
