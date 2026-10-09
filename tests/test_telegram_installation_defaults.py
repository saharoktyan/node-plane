import copy
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.routers import admin_installation_defaults as defaults
from telegram_client.routers import admin_nodes as nodes
from telegram_client.routers.states import NodeDraftState
from tests import test_telegram_node_templates as fixture


class TelegramInstallationDefaultsTests(IsolatedAsyncioTestCase):
    asyncSetUp = fixture.NodeTemplateTests.asyncSetUp
    asyncTearDown = fixture.NodeTemplateTests.asyncTearDown
    message = fixture.NodeTemplateTests.message

    async def test_advanced_table_stays_collapsed_with_edit_controls_outside(self):
        from telegram_client.i18n import tr
        current = {'revision': 1, 'protocols': ['awg', 'xray'], 'xray_transports': ['tcp', 'xhttp'],
            'settings': {'awg_i1_preset': 'quic', 'awg_port_mode': 'auto', 'awg_interface': 'wg0',
                'xray_sni': 'www.cloudflare.com', 'xray_fingerprint': 'chrome',
                'xray_tcp_port': 443, 'xray_xhttp_port': 8443, 'xray_xhttp_path': '/assets'}}
        for locale in ('en', 'ru'):
            await self.state.update_data(locale=locale, installation_defaults_draft=current,
                installation_defaults_view='main')
            with patch.object(defaults, 'render', new_callable=AsyncMock) as draw:
                await defaults.show(self.query, self.bot, self.state)
                screen, rows = draw.call_args.args[2:4]
                details = next(b for b in screen.rich(rows).blocks if b.type == 'details')
                self.assertEqual([b.type for b in details.blocks], ['table'])
                advanced = next(s for s in screen.sections if s.collapsed)
                values = dict(advanced.tables[0].rows)
                self.assertEqual(values[tr(locale, 'nodes.settings.field.awg_port')], tr(locale, 'defaults.auto_value'))
                self.assertNotIn(tr(locale, 'nodes.settings.field.awg_interface'), values)
                self.assertEqual(values[tr(locale, 'nodes.settings.field.xray_tcp_port')], '443')
                self.assertNotIn('—', values.values())
                callbacks = [b.callback_data for row in screen.fallback_rows(rows) for b in row]
                self.assertIn('idefault:advanced', callbacks)
                self.query.data = 'idefault:advanced'
                await defaults.defaults_cb(self.query, self.bot, self.backend, self.state)
                screen, rows = draw.call_args.args[2:4]
                callbacks = [b.callback_data for row in screen.fallback_rows(rows) for b in row]
                self.assertIn('idefault:field:xray_sni', callbacks)
                self.assertNotIn('idefault:field:awg_interface', callbacks)
                self.assertEqual(rows[-1][0].callback_data, 'idefault:main')
                blocks = screen.rich(rows).blocks
                self.assertFalse(any(b.type == 'details' for b in blocks))
                xray = next(s for s in screen.sections if s.title == tr(locale, 'protocol.xray'))
                table = xray.tables[0].rich()
                self.assertEqual(table.cells[1][0].text.type, 'button')
                self.assertEqual(table.cells[1][0].text.button.callback_data, 'idefault:field:xray_sni')
                self.assertEqual(table.cells[1][0].text.button.style, 'link')
                self.assertFalse(xray.rows)

    async def test_existing_local_node_hides_local_choice_and_rejects_old_callback(self):
        self.backend.node_creation_options.return_value['local_available'] = False
        await nodes.new_node_cb(self.query, self.bot, self.backend, self.state)
        callbacks = [b.callback_data for row in self.draw.call_args.args[3] for b in row]
        self.assertNotIn('wizard_transport:local', callbacks)
        self.query.data = 'wizard_transport:local'
        await nodes.wizard_transport_cb(self.query, self.bot, self.state)
        self.assertEqual(await self.state.get_state(), NodeDraftState.waiting_for_transport.state)
        self.assertNotIn('transport', (await self.state.get_data())['wizard_data'])

    async def test_wizard_snapshots_custom_defaults_and_review_shows_transport_and_preset(self):
        self.backend.node_creation_options.return_value['defaults'] = {'revision': 4,
            'protocols': ['awg', 'xray'], 'xray_transports': ['xhttp'],
            'settings': {'awg_i1_preset': 'dns', 'awg_port_mode': 'auto'}}
        await nodes.new_node_cb(self.query, self.bot, self.backend, self.state)
        draft = (await self.state.get_data())['wizard_data']
        self.assertEqual(draft['settings']['awg_i1_preset'], 'dns')
        draft.update(key='ssh1', title='SSH', region='Europe', flag='', transport='ssh',
            ssh_target='root@ssh.example', public_host='ssh.example')
        await self.state.update_data(wizard_data=draft)
        await nodes.render_wizard_summary(123, self.bot, self.state, 77)
        text = self.draw.call_args.args[2].plain()
        self.assertIn('dns', text)
        self.assertIn('xhttp', text)
        self.assertNotIn('tcp, xhttp', text)
        await nodes.wizard_save_cb(self.query, self.bot, self.backend, self.state)
        body = self.backend.create_node.call_args.args[1]
        self.assertEqual(body['xray_transports'], ['xhttp'])
        self.assertEqual(body['settings']['awg_i1_preset'], 'dns')

    async def test_default_preset_is_a_draft_until_save_and_auto_clears_manual_port(self):
        current = {'revision': 3, 'protocols': ['awg', 'xray'], 'xray_transports': ['tcp', 'xhttp'],
            'settings': {'awg_i1_preset': 'quic', 'awg_port_mode': 'manual', 'awg_port': 51820}}
        self.backend.request.return_value = copy.deepcopy(current)
        self.query.data = 'idefault:open'
        with patch.object(defaults, 'render', new_callable=AsyncMock) as draw:
            await defaults.defaults_cb(self.query, self.bot, self.backend, self.state)
            self.backend.request.reset_mock()
            self.query.data = 'idefault:preset:dns'
            await defaults.defaults_cb(self.query, self.bot, self.backend, self.state)
            self.backend.request.assert_not_awaited()
            draft = (await self.state.get_data())['installation_defaults_draft']
            self.assertNotIn('awg_port', draft['settings'])
            self.assertEqual(draft['settings']['awg_port_mode'], 'auto')
            self.query.data = 'idefault:save'
            self.backend.request.return_value = dict(draft, revision=4)
            await defaults.defaults_cb(self.query, self.bot, self.backend, self.state)
        call = self.backend.request.call_args
        self.assertEqual(call.args, ('PUT', defaults.PATH))
        self.assertEqual(call.kwargs['revision'], 3)
        self.assertEqual(call.kwargs['body']['settings']['awg_i1_preset'], 'dns')
        self.assertTrue(draw.call_args.args[2].embedded_buttons)

    async def test_manual_port_entry_only_updates_draft(self):
        await self.state.update_data(installation_defaults_draft={'revision': 1, 'protocols': ['awg'],
            'xray_transports': [], 'settings': {'awg_i1_preset': 'dns', 'awg_port_mode': 'auto'}},
            installation_defaults_field='awg_port')
        await self.state.set_state(defaults.DefaultField.waiting)
        with patch.object(defaults, 'render', new_callable=AsyncMock):
            await defaults.field_text(self.message('51111'), self.bot, self.state)
        self.backend.request.assert_not_awaited()
        draft = (await self.state.get_data())['installation_defaults_draft']
        self.assertEqual((draft['settings']['awg_port_mode'], draft['settings']['awg_port']), ('manual', 51111))
