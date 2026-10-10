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

    async def test_server_links_preserve_update_page_and_callbacks_fit_telegram_limit(self):
        key = 'very-long-server-code-with-suffix'
        origin = 'update_nodes:11111111-2222-4333-8444-555555555555:2'
        callbacks = await updates.node_links(self.state, [{'node_key': key}], origin)
        callback = callbacks[key]
        self.assertLessEqual(len(callback.encode()), 64)
        self.query.data = callback
        from telegram_client.routers import admin_nodes
        with patch.object(admin_nodes, 'show_admin_node', new_callable=AsyncMock) as show:
            await updates.update_node_cb(self.query, self.bot, object(), self.state)
        self.assertEqual(show.await_args.args[3], key)
        self.assertEqual(self.state_data['node_return'], {'node_key': key, 'callback': origin})

    async def test_large_fleet_keeps_every_visible_node_link_on_first_and_last_page(self):
        nodes = [{'key': f'n{i:03}', 'title': f'Node {i:03}'} for i in range(150)]
        for page, expected in ((0, 'n000'), (14, 'n140')):
            callbacks = await updates.node_links(self.state, nodes, f'fleet_nodes:{page}')
            self.assertEqual(len(callbacks), 10)
            token = callbacks[expected].split(':')[1]
            self.assertEqual(self.state_data['update_node_links'][token],
                             {'node_key': expected, 'callback': f'fleet_nodes:{page}'})


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
            auto_buttons = [b for row in screen.fallback_rows(rows) for b in row
                if b.callback_data == 'upd_act:auto_check']
            self.assertEqual(len(auto_buttons),1)
            self.assertEqual(auto_buttons[0].style,'primary')
            self.assertEqual(len(screen.sections[0].tables[0].rows), 4)
            self.assertTrue(screen.sections[2].collapsed)
            self.assertEqual(len(screen.sections[2].sections[0].sections), 10)
            self.assertTrue(all(not section.collapsed for section in screen.sections[3:]))
            self.assertIn('stable · old', screen.lines)
            callbacks = [b.callback_data for row in screen.fallback_rows(rows) for b in row]
            for action in ('ufleet', 'uv_page:0', 'upd_act:branch_menu', 'upd_act:run', 'upd_act:check'):
                self.assertIn(action, callbacks)
            self.assertNotIn('upd_act:cleanup_menu', callbacks)
            self.assertEqual(screen.sections[0].inline_rows[0][-1].callback_data, 'upd_act:check')
            self.assertEqual([b.size for b in screen.rich(rows).blocks if b.type == 'heading'][0], 1)
            with patch.object(updates, 'render', new_callable=AsyncMock) as draw:
                await updates.show_overview(self.query, self.bot, backend, self.state, 1, opened=True)
            servers = draw.call_args.args[2].sections[2]
            self.assertTrue(servers.is_open)
            self.assertEqual(len(servers.sections[0].sections), 1)
            self.assertEqual(servers.rows[0][0].text, '←')
            self.assertEqual(servers.sections[0].title, '')
            self.assertEqual(servers.sections[0].sections[0].title, '')

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
        token = servers.sections[0].sections[0].inline_rows[0][0].callback_data.split(':')[1]
        self.assertEqual(self.state_data['update_node_links'][token],
                         {'node_key': 'old', 'callback': 'updates_nodes:0'})
        self.assertEqual(len(screen.lines), 1)
        self.assertEqual(updates.state_label('en', 'current'), 'Up to date')

    async def test_compact_fleet_combines_runtime_and_preserves_inline_node_navigation(self):
        fleet = dict(desired_version='v', desired_commit='hash', driver_status='current',
            driver={}, agents_required=False, runtimes_required=True, latest_job=None,
            nodes=[dict(key='n1', title='Latvia', region='Europe', agent_status='current', runtime_status='required')])
        backend = SimpleNamespace(update_rollout=AsyncMock(return_value=fleet))
        with patch.object(updates, 'render', new_callable=AsyncMock) as draw:
            await updates.show_fleet(self.query, self.bot, backend, self.state)
        screen, rows = draw.call_args.args[2:4]
        self.assertNotIn('ufleet', [b.callback_data for row in rows for b in row])
        entry = screen.sections[0].sections[0].sections[0]
        self.assertEqual(entry.inline_rows[0][2], updates.state_label('en', 'required'))
        self.assertEqual(entry.title, '')
        token = entry.inline_rows[0][0].callback_data.split(':')[1]
        self.assertEqual(self.state_data['update_node_links'][token]['callback'], 'fleet_nodes:0')
        self.assertNotIn('Runtime:', screen.plain())
        screen.rich(rows)

    async def test_dismiss_hides_only_that_result_and_new_job_remains_visible(self):
        latest = dict(id='j1', status='succeeded', kind='stack', items=[], result={})
        overview = {'latest_job': latest, 'dismissed_job_ids': []}
        async def dismiss(*args, **kwargs):
            overview['dismissed_job_ids'].append('j1')
        backend = SimpleNamespace(update_job=AsyncMock(return_value=latest), request=AsyncMock(side_effect=dismiss),
            updates_overview=AsyncMock(return_value=overview),
            update_rollout=AsyncMock(return_value={'nodes': [], 'driver_status': 'current'}))
        self.query.data = 'update_dismiss:j1'
        with patch.object(updates, 'render', new_callable=AsyncMock) as draw:
            await updates.update_tools_cb(self.query, self.bot, backend, self.state)
        backend.request.assert_awaited_once_with('POST', '/api/v1/system/updates/jobs/j1/dismiss',
            telegram_user_id=self.query.from_user.id)
        screen, rows = draw.call_args.args[2:4]
        self.assertNotIn('update_job:j1', [b.callback_data for row in screen.fallback_rows(rows) for b in row])
        latest['id'] = 'j2'
        with patch.object(updates, 'render', new_callable=AsyncMock) as draw:
            await updates.show_overview(self.query, self.bot, backend, self.state)
        screen, rows = draw.call_args.args[2:4]
        self.assertIn('update_job:j2', [b.callback_data for row in screen.fallback_rows(rows) for b in row])

    async def test_check_row_reports_latest_version_and_completed_check_without_hash(self):
        overview = dict(branch='dev', current_version='1.0', current_label='1.0 · deadbeef',
            remote_version='1.0', last_checked_at='2026-10-10T08:00:00Z', update_available=False)
        backend = SimpleNamespace(updates_overview=AsyncMock(return_value=overview),
            update_rollout=AsyncMock(return_value={'nodes': [], 'driver_status': 'current'}))
        with patch.object(updates, 'render', new_callable=AsyncMock) as draw:
            await updates.show_overview(self.query, self.bot, backend, self.state)
        screen, rows = draw.call_args.args[2:4]
        self.assertEqual(screen.lines, ('dev · 1.0',))
        self.assertIn('1.0 · Last check: 2026-10-10 08:00 UTC', screen.plain())
        callbacks = [b.callback_data for row in screen.fallback_rows(rows) for b in row]
        self.assertEqual(callbacks.count('upd_act:check'), 1)
        self.assertNotIn('updates', callbacks)
        blocks = screen.rich(rows).blocks
        self.assertTrue(any(b.type == 'paragraph' and any(getattr(t, 'type', None) == 'button'
            for t in b.text) for b in blocks))

    async def test_branch_change_requires_explicit_confirmation(self):
        from telegram_client.routers import admin_settings
        backend = SimpleNamespace(updates_overview=AsyncMock(return_value={'branch': 'main'}),
            update_preferences=AsyncMock())
        self.query.data = 'upd_act:branch_dev'
        with patch.object(admin_settings, 'render', new_callable=AsyncMock) as draw:
            await admin_settings.update_action_cb(self.query,
                admin_settings.UpdateActionCallback(action='branch_dev'), self.bot, backend, self.state)
        backend.update_preferences.assert_not_awaited()
        self.assertEqual(draw.call_args.args[3][0][1].style, 'danger')
        action = draw.call_args.args[3][0][1].callback_data.split(':', 1)[1]
        with patch.object(admin_settings, 'show_update_branches', new_callable=AsyncMock):
            await admin_settings.update_action_cb(self.query,
                admin_settings.UpdateActionCallback(action=action), self.bot, backend, self.state)
        backend.update_preferences.assert_awaited_once_with(self.query.from_user.id, {'branch': 'dev'})

    async def test_downgrade_confirmation_warns_and_does_not_leak_ui_flags_to_api(self):
        page = {'branch': 'dev', 'offset': 0, 'items': [dict(allowed=True, action='downgrade',
            ref='v0.1', version='0.1', reason='downgrade')]}
        self.state_data.update(update_catalog=page, update_catalog_nonce='nonce')
        self.query.data = 'uv_select:nonce:0'
        with patch.object(updates, 'render', new_callable=AsyncMock) as draw:
            await updates.update_tools_cb(self.query, self.bot, object(), self.state)
        self.assertEqual(draw.call_args.args[3][0][1].style, 'danger')
        self.assertIn(updates.tr('en', 'updates.rich.downgrade_warning'), draw.call_args.args[2].lines)
        self.assertNotIn('_dangerous', self.state_data['update_draft']['body'])

    async def test_completed_update_does_not_override_current_version_state(self):
        overview = {'update_available': False, 'latest_job': {'id': 'old', 'status': 'succeeded',
            'result': {'components': dict.fromkeys(('backend', 'worker', 'driver', 'telegram'), 'succeeded')}}}
        fleet = {'nodes': [], 'driver_status': 'required'}
        backend = SimpleNamespace(updates_overview=AsyncMock(return_value=overview),
            update_rollout=AsyncMock(return_value=fleet))
        for lang in ('en', 'ru'):
            self.state_data['locale'] = lang
            for available in (False, True):
                overview['update_available'] = available
                with patch.object(updates, 'render', new_callable=AsyncMock) as draw:
                    await updates.show_overview(self.query, self.bot, backend, self.state)
                table = draw.call_args.args[2].sections[0].tables[0]
                expected = updates.state_label(lang, 'required' if available else 'current')
                self.assertEqual([row[1] for row in table.rows],
                    [expected, expected, updates.state_label(lang, 'required'), expected])
                self.assertEqual(table.headers[1], updates.tr(lang, 'updates.rich.version_state'))
        overview['latest_job']['status'] = 'running'
        overview['latest_job']['result']['components']['backend'] = 'running'
        with patch.object(updates, 'render', new_callable=AsyncMock) as draw:
            await updates.show_overview(self.query, self.bot, backend, self.state)
        self.assertEqual(draw.call_args.args[2].sections[0].tables[0].rows[0][1],
            updates.state_label('ru', 'running'))

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
        token = screen.sections[1].sections[0].sections[0].inline_rows[0][0].callback_data.split(':')[1]
        self.assertEqual(self.state_data['update_node_links'][token],
                         {'node_key': 'lv1', 'callback': 'update_nodes:j1:0'})
        self.assertIn(updates.tr('en', 'updates.rich.partial_note'), screen.lines)
        self.assertTrue(screen.sections[-1].collapsed)
        screen.rich(rows)
