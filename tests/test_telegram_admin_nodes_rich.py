"""Server presentation, region wizard and preservation of operational actions."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.routers import admin_nodes as nodes, admin_node_tools as tools
from telegram_client.routers.states import NodeDraftState
from telegram_client.i18n import tr


class AdminNodeRichTests(IsolatedAsyncioTestCase):
    async def test_deleting_card_exposes_progress_instead_of_mutation_controls(self):
        for locale in ('ru', 'en'):
            self.data['locale'] = locale
            for status in ('queued', 'running', 'blocked'):
                self.overview.update(removal_status=status,
                    state='deletion_blocked' if status == 'blocked' else 'deleting')
                with patch.object(nodes, 'render', new_callable=AsyncMock) as draw:
                    await nodes.show_admin_node(123, 123, 77, 'msk1', self.bot, self.backend, self.state)
                callbacks = self.callbacks(draw)
                self.assertIn('remove_progress:msk1', callbacks)
                self.assertFalse(any(c.startswith(('node_settings:', 'bootstrap_menu:', 'node_manage:')) for c in callbacks))
                self.assertIn(tr(locale, 'nodes.card.state.' + self.overview['state']), draw.call_args.args[2].plain())
                with patch.object(nodes, 'render', new_callable=AsyncMock) as draw:
                    await nodes.show_admin_nodes(123, 123, 77, self.bot, self.backend, self.state)
                labels = [button.text for row in draw.call_args.args[2].fallback_rows(draw.call_args.args[3]) for button in row]
                self.assertTrue(any(tr(locale, 'nodes.card.state.' + self.overview['state']) in label for label in labels))

    def setUp(self):
        self.data = {'locale': 'en', 'control_message_id': 77}
        async def get_data():
            return dict(self.data)
        async def update_data(**values):
            self.data.update(values)
        async def clear():
            self.data.clear()
        self.state = SimpleNamespace(get_data=get_data, update_data=update_data, clear=clear, set_state=AsyncMock())
        self.bot = SimpleNamespace()
        self.query = SimpleNamespace(data='', answer=AsyncMock(), from_user=SimpleNamespace(id=123),
            message=SimpleNamespace(message_id=77, chat=SimpleNamespace(id=123)))
        self.node = {'key': 'msk1', 'title': 'Moscow #1', 'region': 'Europe', 'flag': '🇷🇺',
            'protocols': ['awg', 'xray'], 'xray_transports': ['tcp', 'xhttp'], 'transport': 'ssh',
            'ssh_target': 'root@private.example.test', 'notes': '', 'desired_revision': 3,
            'applied_revision': 2, 'settings': {'public_host': 'node.example.test', 'awg_port': 51820}}
        self.overview = {'state': 'changes_pending', 'ready': 2, 'pending': 1, 'failed': 0,
            'attention': 0, 'access_total': 3, 'settings_complete': True, 'last_job': None}
        self.backend = SimpleNamespace(request=AsyncMock(return_value=self.node),
            node_overview=AsyncMock(return_value=self.overview), node_services=AsyncMock(),
            admin_nodes=AsyncMock(return_value={'items': [{**self.node, 'overview': self.overview}], 'next_cursor': None}))

    def callbacks(self, draw):
        screen, rows = draw.call_args.args[2:4]
        return [b.callback_data for row in screen.fallback_rows(rows) for b in row]

    async def test_landing_card_has_common_actions_and_no_hidden_agent_rpc(self):
        for locale in ('ru', 'en'):
            self.data['locale'] = locale
            with patch.object(nodes, 'render', new_callable=AsyncMock) as draw:
                await nodes.show_admin_node(123, 123, 77, 'msk1', self.bot, self.backend, self.state)
            screen, rows = draw.call_args.args[2:4]
            self.assertEqual(screen.title, 'Europe · 🇷🇺 Moscow #1')
            self.assertNotIn('private.example.test', screen.plain())
            self.assertNotIn('msk1', screen.plain())
            self.assertNotIn(tr(locale, 'nodes.rich.technical'), screen.plain())
            callbacks = self.callbacks(draw)
            self.assertIn('node_manage:msk1', callbacks)
            self.assertNotIn('bootstrap_menu:msk1', callbacks)
            self.assertNotIn('node_section:awg:msk1', callbacks)
            self.assertNotIn('node_section:xray:msk1', callbacks)
            settings_buttons = [b for row in screen.fallback_rows(rows) for b in row if b.callback_data == 'node_settings:msk1']
            self.assertEqual(len(settings_buttons), 1)
            self.assertEqual(settings_buttons[0].style, 'primary')
            self.assertFalse(any(c.startswith('apply_node:') for c in callbacks))
            self.assertFalse(any(c.startswith('node_maintenance:') for c in callbacks))
            self.assertEqual([b.type for b in screen.rich(rows).blocks[-2:]], ['divider', 'buttons'])
        self.backend.node_services.assert_not_awaited()

    async def test_running_or_blocked_job_has_prominent_entry(self):
        for status in ('running', 'blocked'):
            self.overview['last_job'] = {'id': 'j1', 'status': status}
            with patch.object(nodes, 'render', new_callable=AsyncMock) as draw:
                await nodes.show_admin_node(123, 123, 77, 'msk1', self.bot, self.backend, self.state)
            action = draw.call_args.args[2].sections[0].rows[0][0]
            self.assertEqual((action.callback_data, action.style), ('node_job:j1', 'primary'))

    async def test_settings_has_one_conditional_apply_and_no_technical_dump(self):
        for pending in (False, True):
            self.node['applied_revision'] = 2 if pending else 3
            with patch.object(nodes, 'render', new_callable=AsyncMock) as draw:
                await nodes.show_node_settings(123, 123, 77, 'msk1', self.bot, self.backend, self.state)
            callbacks = self.callbacks(draw)
            self.assertEqual(sum(c.startswith('apply_node:') for c in callbacks), int(pending))
            self.assertIn('node_section:connection:msk1', callbacks)
            self.assertIn('node_section:general:msk1', callbacks)
            self.assertNotIn('node_tools:msk1', callbacks)
            self.assertNotIn('51820', draw.call_args.args[2].plain())
            self.assertNotIn(tr('en', 'nodes.rich.applied'), draw.call_args.args[2].plain())

    async def test_applied_card_has_no_permanent_success_notice(self):
        self.node['applied_revision'] = self.node['desired_revision']
        self.overview['state'] = 'applied_unverified'
        with patch.object(nodes, 'render', new_callable=AsyncMock) as draw:
            await nodes.show_admin_node(123, 123, 77, 'msk1', self.bot, self.backend, self.state)
        self.assertNotIn(tr('en', 'nodes.rich.applied'), draw.call_args.args[2].plain())
        self.assertNotIn(tr('en', 'nodes.rich.pending'), draw.call_args.args[2].plain())
        self.assertNotIn(tr('en', 'nodes.card.state.applied_unverified'), draw.call_args.args[2].plain())
        with patch.object(nodes, 'render', new_callable=AsyncMock) as draw:
            await nodes.show_admin_nodes(123, 123, 77, self.bot, self.backend, self.state)
        labels = [b.text for row in draw.call_args.args[2].fallback_rows(draw.call_args.args[3]) for b in row]
        self.assertIn('🇷🇺 Moscow #1', labels)

    async def test_protocol_settings_have_tables_and_all_existing_edit_actions(self):
        expected = {'general': {'title', 'flag', 'region', 'notes'}, 'connection': {'public_host'},
            'awg': {'awg_public_host', 'awg_port', 'awg_interface', 'awg_i1_preset'},
            'xray': {'xray_host', 'xray_sni', 'xray_tcp_port', 'xray_xhttp_port', 'xray_fingerprint', 'xray_xhttp_path'}}
        for locale in ('ru', 'en'):
            self.data['locale'] = locale
            for section, fields in expected.items():
                with patch.object(tools, 'render', new_callable=AsyncMock) as draw:
                    await tools.show_section(123, 123, 77, section, 'msk1', self.bot, self.backend, self.state)
                screen, rows = draw.call_args.args[2:4]
                self.assertTrue(screen.embedded_buttons)
                callbacks = self.callbacks(draw)
                actual = {c.split(':')[-1] for c in callbacks if c.startswith('edit_node_field:')}
                self.assertEqual(actual, fields)
                self.assertTrue(any(c.startswith('apply_node:') for c in callbacks))
                groups = screen.sections[0].sections if section in {'awg', 'xray'} else screen.sections
                self.assertTrue(any(s.tables for s in groups))
                headings = [b.size for b in screen.rich(rows).blocks if b.type == 'heading']
                self.assertEqual(headings[0], 1)
                self.assertIn(2, headings)
                if section in {'awg', 'xray'}:
                    self.assertIn(3, headings)
                self.assertEqual(rows[-1][0].callback_data, 'node_settings:msk1')

    async def test_region_presets_and_other_keep_node_flag_and_draft(self):
        self.data['wizard_data'] = {'key': 'n1', 'title': 'Node 1', 'flag': '🇱🇻'}
        for locale in ('ru', 'en'):
            self.data['locale'] = locale
            with patch.object(nodes, 'render', new_callable=AsyncMock) as draw:
                await nodes.render_wizard_step(123, self.bot, self.state, 'region', 77)
            self.state.set_state.assert_awaited_with(None)
            screen, rows = draw.call_args.args[2:4]
            self.assertTrue(screen.embedded_buttons)
            self.assertEqual(rows[0][0].text, '🌍 ' + tr(locale, 'region.europe'))
            self.assertEqual(len(rows[0]), 2)
            self.assertEqual(rows[-2][0].callback_data, 'wizard_region:other')
            self.assertEqual(rows[-1][0].callback_data, 'wizard_back:title')
        self.query.data = 'wizard_region:asia'
        with patch.object(nodes, 'render_wizard_step', new_callable=AsyncMock) as next_step:
            await nodes.wizard_region_cb(self.query, self.bot, self.state)
        self.assertEqual(self.data['wizard_data']['region'], 'Asia')
        self.assertEqual(self.data['wizard_data']['flag'], '🇱🇻')
        self.assertEqual(next_step.call_args.args[3], 'flag')
        self.query.data = 'wizard_region:other'
        with patch.object(nodes, 'render', new_callable=AsyncMock) as draw:
            await nodes.wizard_region_cb(self.query, self.bot, self.state)
        self.state.set_state.assert_awaited_with(NodeDraftState.waiting_for_region)
        self.assertEqual(draw.call_args.args[3][0][0].callback_data, 'wizard_back:region')
        message = SimpleNamespace(text='Custom region', from_user=SimpleNamespace(id=123), chat=SimpleNamespace(id=123, type='private'), delete=AsyncMock())
        with patch.object(nodes, 'render_wizard_step', new_callable=AsyncMock):
            await nodes.process_wizard_region(message, self.bot, self.state)
        self.assertEqual(self.data['wizard_data']['region'], 'Custom region')

    async def test_server_list_uses_backend_region_order_and_embedded_rows(self):
        with patch.object(nodes, 'render', new_callable=AsyncMock) as draw:
            await nodes.show_admin_nodes(123, 123, 77, self.bot, self.backend, self.state)
        self.backend.admin_nodes.assert_awaited_once_with(123, cursor=None, search=None, limit=10, order='region')
        screen, rows = draw.call_args.args[2:4]
        self.assertEqual(screen.sections[1].title, 'Europe')
        self.assertTrue(screen.sections[1].sections[0].rows[0][0].text.startswith('🇷🇺 Moscow #1'))
        self.assertEqual(rows[-1][0].callback_data, 'admin_menu')

    async def test_wizard_choices_and_review_keep_draft_and_navigation(self):
        self.data['wizard_data'] = {**self.node, 'public_host': 'node.example.test'}
        for locale in ('en', 'ru'):
            self.data['locale'] = locale
            with patch.object(nodes, 'render', new_callable=AsyncMock) as draw:
                await nodes.render_wizard_transport(123, self.bot, self.state, 77)
            self.assertEqual([b.callback_data for b in draw.call_args.args[3][0]],
                             ['wizard_transport:ssh', 'wizard_transport:local'])
            with patch.object(nodes, 'render', new_callable=AsyncMock) as draw:
                await nodes.render_wizard_protocols(123, self.bot, self.state, 77)
            rows = draw.call_args.args[3]
            self.assertEqual(len(rows[0]), 2)
            self.assertTrue(all(b.style == 'primary' for b in rows[0]))
            self.assertEqual([b.callback_data for b in rows[-1]], ['wizard_back:public_host', 'wizard_proto:done'])
            with patch.object(nodes, 'render', new_callable=AsyncMock) as draw:
                await nodes.render_wizard_summary(123, self.bot, self.state, 77)
            screen, rows = draw.call_args.args[2:4]
            self.assertEqual(len(screen.sections), 4)
            self.assertTrue(screen.sections[-1].collapsed)
            self.assertIn('private.example.test', screen.sections[-1].lines[-1])
            self.assertEqual(rows[-1][-1].callback_data, 'wizard_save')
            self.assertEqual(self.data['wizard_data']['key'], 'msk1')
            screen.rich(rows)

    async def test_port_result_has_table_and_collapsed_operation_identity(self):
        job = {'id': 'job1', 'node_key': 'msk1', 'status': 'succeeded', 'action': 'check_ports',
               'result': {'ports': [{'port': 443, 'protocol': 'tcp', 'status': 'free'}]}}
        for locale in ('en', 'ru'):
            self.data['locale'] = locale
            with patch.object(tools, 'render', new_callable=AsyncMock) as draw:
                await tools.show_job(123, 123, 77, job, self.bot, self.state)
            screen, rows = draw.call_args.args[2:4]
            self.assertEqual(screen.sections[0].tables[0].rows[0][:2], ('443', 'tcp'))
            self.assertTrue(screen.sections[-1].collapsed)
            self.assertIn('job1', screen.sections[-1].lines[0])
            self.assertEqual(rows[-1][0].callback_data, 'admin_node:msk1')
            screen.rich(rows)

    async def test_rollout_progress_retains_refresh_and_collapses_identifier(self):
        backend = SimpleNamespace(agent_rollout=AsyncMock(return_value={
            'id': 'task1', 'node_key': 'msk1', 'status': 'running'}))
        for locale in ('en', 'ru'):
            self.data['locale'] = locale
            with patch.object(nodes, 'render', new_callable=AsyncMock) as draw:
                await nodes.show_rollout_status(123, 123, 77, 'task1', self.bot, backend, self.state)
            screen, rows = draw.call_args.args[2:4]
            self.assertIn(tr(locale, 'nodes.rich.independent'), screen.lines)
            self.assertTrue(screen.sections[0].collapsed)
            self.assertIn('rollout_status:task1', self.callbacks(draw))
            screen.rich(rows)

    async def test_unreachable_removal_keeps_recovery_and_registry_only_choice(self):
        from telegram_client.backend import BackendError
        backend = SimpleNamespace(remove_node_step=AsyncMock(side_effect=BackendError('node_agent_unavailable', 503)))
        with patch.object(tools, 'render', new_callable=AsyncMock) as draw:
            await tools.advance_removal(123, 123, 77, 'msk1', self.bot, backend, self.state)
        screen, rows = draw.call_args.args[2:4]
        self.assertIn(tr('en', 'node_tools.unreachable'), screen.lines)
        self.assertIn('remove_retry:msk1', self.callbacks(draw))
        self.assertTrue(any('registry' in value for value in self.callbacks(draw)))
        self.assertEqual(self.data['unreachable_removal_node'], 'msk1')
        screen.rich(rows)

    async def test_connection_choices_return_to_connection_section(self):
        with patch.object(nodes, 'render', new_callable=AsyncMock) as draw:
            await nodes.show_node_connection(123, 123, 77, 'msk1', self.bot, self.backend, self.state)
        screen, rows = draw.call_args.args[2:4]
        choices = screen.sections[0].rows[0]
        self.assertEqual(len(choices), 2)
        self.assertEqual(choices[1].style, 'primary')
        self.assertEqual(rows[-1][0].callback_data, 'node_section:connection:msk1')
        self.assertTrue(screen.sections[1].collapsed)
        screen.rich(rows)

    async def test_runtime_view_keeps_versions_visible_and_commits_collapsed(self):
        self.backend.node_services.return_value = {
            'runtime_version': '0.4.3-alpha.30', 'runtime_commit': 'old1234',
            'desired_runtime_version': '0.4.3-alpha.31', 'desired_runtime_commit': 'new1234',
            'runtime_drift': True}
        self.query.data = 'node_view:runtime:msk1'
        with patch.object(tools, 'render', new_callable=AsyncMock) as draw:
            await tools.view_cb(self.query, self.bot, self.backend, self.state)
        screen, rows = draw.call_args.args[2:4]
        self.assertEqual(screen.sections[0].tables[0].rows[0][1], '0.4.3-alpha.30')
        self.assertTrue(screen.sections[1].collapsed)
        self.assertIn('node_action:sync_runtime:msk1', self.callbacks(draw))
        screen.rich(rows)

    async def test_draft_survives_navigation_and_reset_does_not_write_backend(self):
        self.backend.edit_node = AsyncMock()
        await nodes.change_node_draft(123, 'msk1', {'settings': {**self.node['settings'], 'awg_port': 51821}}, self.backend, self.state)
        await nodes._clear_node_flow(self.state)
        with patch.object(tools, 'render', new_callable=AsyncMock) as draw:
            await tools.show_section(123, 123, 77, 'awg', 'msk1', self.bot, self.backend, self.state)
        self.assertIn('node_draft_save:msk1', self.callbacks(draw))
        self.assertIn('node_draft_reset:msk1', self.callbacks(draw))
        self.query.data = 'node_draft_reset:msk1'
        with patch.object(tools, 'render', new_callable=AsyncMock) as draw:
            await nodes.reset_node_draft_cb(self.query, self.bot, self.backend, self.state)
        self.assertIsNone(self.data['node_settings_draft'])
        self.assertNotIn('node_draft_save:msk1', self.callbacks(draw))
        self.backend.edit_node.assert_not_awaited()

    async def test_save_draft_uses_captured_revision_then_applies_once(self):
        self.backend.edit_node = AsyncMock(return_value={**self.node, 'desired_revision': 4})
        await nodes.change_node_draft(123, 'msk1', {'region': 'Asia'}, self.backend, self.state)
        self.node['desired_revision'] = 9
        self.query.data = 'node_draft_save:msk1'
        with patch.object(nodes, 'apply_node', new_callable=AsyncMock) as apply:
            await nodes.save_node_draft_cb(self.query, self.bot, self.backend, self.state)
        self.assertEqual(self.backend.edit_node.await_args.args, (123, 'msk1', 3, {'region': 'Asia'}))
        self.assertIsNone(self.data['node_settings_draft'])
        apply.assert_awaited_once()
        self.assertEqual(apply.await_args.kwargs['revision'], 4)

    async def test_protocol_in_use_keeps_draft_and_explains_save_failure(self):
        from telegram_client.backend import BackendError
        self.backend.edit_node = AsyncMock(side_effect=BackendError('node_protocol_in_use', 409))
        await nodes.change_node_draft(123, 'msk1', {'protocols': ['awg'], 'xray_transports': []}, self.backend, self.state)
        self.query.data = 'node_draft_save:msk1'
        with patch.object(nodes, 'render', new_callable=AsyncMock) as draw, patch.object(nodes, 'apply_node', new_callable=AsyncMock) as apply:
            await nodes.save_node_draft_cb(self.query, self.bot, self.backend, self.state)
        self.assertIn(tr('en', 'nodes.draft.in_use'), draw.call_args.args[2].lines)
        self.assertEqual(self.data['node_settings_draft']['values']['protocols'], ['awg'])
        apply.assert_not_awaited()

    async def test_protocol_draft_is_selected_across_views_and_last_transport_is_required(self):
        self.node['xray_transports'] = ['tcp', 'xhttp']
        with patch.object(nodes, 'render', new_callable=AsyncMock) as draw:
            await nodes.toggle_node_feature(123, 123, 77, 'msk1', 'tcp', True, self.bot, self.backend, self.state)
        self.assertEqual(self.data['node_settings_draft']['values']['xray_transports'], ['xhttp'])
        with patch.object(tools, 'render', new_callable=AsyncMock):
            await tools.show_section(123, 123, 77, 'xray', 'msk1', self.bot, self.backend, self.state)
        with patch.object(nodes, 'render', new_callable=AsyncMock) as draw:
            await nodes.show_node_protocols(123, 123, 77, 'msk1', self.bot, self.backend, self.state)
        transport_buttons = draw.call_args.args[3][1]
        self.assertIsNone(transport_buttons[0].style)
        self.assertEqual(transport_buttons[1].style, 'primary')
        with patch.object(nodes, 'render', new_callable=AsyncMock) as draw:
            await nodes.toggle_node_feature(123, 123, 77, 'msk1', 'xhttp', True, self.bot, self.backend, self.state)
        self.assertEqual(self.data['node_settings_draft']['values']['xray_transports'], ['xhttp'])
        self.assertIn(tr('en', 'nodes.draft.transport_required'), draw.call_args.args[2].lines)

    async def test_existing_region_picker_updates_draft_only(self):
        from telegram_client.routers.callbacks import EditNodeFieldCallback
        self.backend.edit_node = AsyncMock()
        with patch.object(nodes, 'render', new_callable=AsyncMock) as draw:
            await nodes.edit_node_field_cb(self.query, EditNodeFieldCallback(node_key='msk1', field='region'), self.bot, self.backend, self.state)
        self.assertTrue(draw.call_args.args[3][0][0].text.startswith('🌍'))
        self.query.data = 'node_region:asia:msk1'
        with patch.object(tools, 'render', new_callable=AsyncMock):
            await nodes.edit_node_region_cb(self.query, self.bot, self.backend, self.state)
        self.assertEqual(self.data['node_settings_draft']['values']['region'], 'Asia')
        self.assertEqual(self.data['node_settings_draft']['values']['flag'], '🇷🇺')
        self.backend.edit_node.assert_not_awaited()

    async def test_reinstall_is_deeper_and_initial_bootstrap_stays_available(self):
        self.query.data = 'node_manage:msk1'
        with patch.object(nodes, 'render', new_callable=AsyncMock) as draw:
            await nodes.node_manage_cb(self.query, self.bot, self.backend, self.state)
        self.assertEqual(draw.call_args.args[3][0][0].text, tr('en', 'nodes.draft.reinstall'))
        self.overview['state'] = 'not_installed'
        self.node['applied_revision'] = 0
        with patch.object(nodes, 'render', new_callable=AsyncMock) as draw:
            await nodes.show_admin_node(123, 123, 77, 'msk1', self.bot, self.backend, self.state)
        self.assertIn('bootstrap_menu:msk1', self.callbacks(draw))

    async def test_new_node_settings_and_stale_apply_button_lead_to_bootstrap(self):
        self.node['applied_revision'] = 0
        self.overview['state'] = 'applying'
        with patch.object(nodes, 'render', new_callable=AsyncMock) as draw:
            await nodes.show_admin_node(123, 123, 77, 'msk1', self.bot, self.backend, self.state)
        self.assertIn('bootstrap_menu:msk1', self.callbacks(draw))
        self.assertFalse(any(c.startswith('apply_node:') for c in self.callbacks(draw)))
        controls = await nodes.draft_controls(self.node, self.state, 'en')
        self.assertEqual(controls[0][0].callback_data, 'bootstrap_menu:msk1')
        self.backend.apply_node_settings = AsyncMock()
        with patch.object(tools, 'show_install', new_callable=AsyncMock) as install:
            await nodes.apply_node(123, 123, 77, 'msk1', self.bot, self.backend, self.state)
        install.assert_awaited_once()
        self.backend.apply_node_settings.assert_not_awaited()

    async def test_initial_bootstrap_only_offers_unfinished_installation_steps(self):
        self.node['applied_revision'] = 0
        for docker in (False, True):
            self.backend.node_services.return_value = {'docker': docker,
                'awg_config_valid': False, 'xray_config_valid': False}
            with patch.object(tools, 'render', new_callable=AsyncMock) as draw:
                await tools.show_install(123, 123, 77, 'msk1', self.bot, self.backend, self.state)
            callbacks = self.callbacks(draw)
            if docker:
                self.assertIn('node_action:bootstrap:msk1', callbacks)
                self.assertNotIn('node_action:install_docker:msk1', callbacks)
            else:
                self.assertIn('node_action:install_docker:msk1', callbacks)

    async def test_unstarted_initial_apply_result_offers_installation_instead_of_spinner(self):
        self.backend.node_settings_operation = AsyncMock(return_value={'status': 'superseded',
            'error_code': 'node_installation_required'})
        with patch.object(nodes, 'render', new_callable=AsyncMock) as draw:
            await nodes.show_node_apply_status(123, 123, 77, 'task', 'msk1', self.bot, self.backend, self.state)
        self.assertIn('bootstrap_menu:msk1', self.callbacks(draw))
        self.assertIn(tr('en', 'nodes.apply.installation_required'), draw.call_args.args[2].lines)
