"""Node creation journeys with presets, manual locations and backward navigation."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey

from telegram_client.backend import BackendError
from telegram_client.i18n import tr
from telegram_client.node_templates import NODE_TEMPLATES
from telegram_client.routers import admin_nodes as nodes
from telegram_client.routers.states import NodeDraftState


class NodeTemplateTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.storage = MemoryStorage()
        self.state = FSMContext(self.storage, StorageKey(bot_id=1, chat_id=123, user_id=123))
        await self.state.update_data(locale='en', control_message_id=77)
        self.bot = SimpleNamespace()
        self.query = SimpleNamespace(data='', answer=AsyncMock(), from_user=SimpleNamespace(id=123),
            message=SimpleNamespace(message_id=77, chat=SimpleNamespace(id=123)))
        self.backend = SimpleNamespace(request=AsyncMock(return_value={'items': [], 'next_cursor': None}),
            create_node=AsyncMock(), node_creation_options=AsyncMock(return_value={
                'local_available': True, 'defaults': {'revision': 1, 'protocols': ['awg', 'xray'],
                    'xray_transports': ['tcp', 'xhttp'], 'settings': {'awg_i1_preset': 'quic', 'awg_port_mode': 'auto'}}}))
        self.drawer = patch.object(nodes, 'render', new_callable=AsyncMock)
        self.draw = self.drawer.start()
        self.addCleanup(self.drawer.stop)

    async def asyncTearDown(self):
        await self.storage.close()

    def message(self, text):
        return SimpleNamespace(text=text, delete=AsyncMock(), from_user=SimpleNamespace(id=123),
            chat=SimpleNamespace(id=123, type='private'))

    async def choose(self, transport, template='lv'):
        await nodes.new_node_cb(self.query, self.bot, self.backend, self.state)
        self.assertEqual(await self.state.get_state(), NodeDraftState.waiting_for_transport.state)
        self.query.data = 'wizard_transport:' + transport
        await nodes.wizard_transport_cb(self.query, self.bot, self.state)
        self.assertEqual(await self.state.get_state(), NodeDraftState.waiting_for_template.state)
        self.query.data = 'wizard_template:' + template
        await nodes.wizard_template_cb(self.query, self.bot, self.backend, self.state)

    async def test_ssh_creation_only_requires_two_address_messages(self):
        await self.choose('ssh')
        self.assertEqual(await self.state.get_state(), NodeDraftState.waiting_for_target.state)
        await nodes.process_wizard_target(self.message('root@node.example.test:22'), self.bot, self.state)
        await nodes.process_wizard_host(self.message('vpn.example.test'), self.bot, self.backend, self.state)
        self.query.data = 'wizard_proto:xray'
        await nodes.wizard_proto_cb(self.query, self.bot, self.backend, self.state)
        self.query.data = 'wizard_proto:done'
        await nodes.wizard_proto_cb(self.query, self.bot, self.backend, self.state)
        await nodes.wizard_save_cb(self.query, self.bot, self.backend, self.state)
        body = self.backend.create_node.call_args.args[1]
        self.assertEqual((body['title'], body['region'], body['flag']), ('Latvia #1', 'Europe', '🇱🇻'))
        self.assertEqual(body['ssh_target'], 'root@node.example.test')
        self.assertEqual(body['settings'], {'public_host': 'vpn.example.test', 'awg_i1_preset': 'quic', 'awg_port_mode': 'auto'})
        self.assertEqual(body['protocols'], ['awg'])
        self.assertRegex(body['key'], r'^lv1-[a-f0-9]{8}$')
        self.assertTrue((await self.state.get_data())['wizard_saved'])

    async def test_local_only_needs_public_address_and_back_keeps_template(self):
        await self.choose('local', 'de')
        self.assertEqual(await self.state.get_state(), NodeDraftState.waiting_for_public_host.state)
        self.assertEqual(self.draw.call_args.args[3][0][0].callback_data, 'wizard_back:template')
        await nodes.process_wizard_host(self.message('local.example.test'), self.bot, self.backend, self.state)
        original = (await self.state.get_data())['wizard_data']
        self.query.data = 'wizard_back:public_host'
        await nodes.wizard_back_cb(self.query, self.bot, self.state)
        self.query.data = 'wizard_back:template'
        await nodes.wizard_back_cb(self.query, self.bot, self.state)
        self.query.data = 'wizard_template:de'
        await nodes.wizard_template_cb(self.query, self.bot, self.backend, self.state)
        self.assertEqual((await self.state.get_data())['wizard_data'], original)
        self.backend.request.assert_awaited_once()
        self.assertIsNone(original['ssh_target'])

    async def test_back_from_ssh_target_returns_to_templates_not_transport(self):
        await self.choose('ssh')
        self.assertEqual(self.draw.call_args.args[3][0][0].callback_data, 'wizard_back:template')
        self.query.data = 'wizard_back:template'
        await nodes.wizard_back_cb(self.query, self.bot, self.state)
        self.assertEqual(self.draw.call_args.args[3][-1][0].callback_data, 'wizard_back:transport')

    async def test_manual_location_still_flows_from_type_to_addresses(self):
        await self.choose('ssh', 'custom')
        self.assertEqual(await self.state.get_state(), NodeDraftState.waiting_for_key.state)
        await nodes.process_wizard_key(self.message('custom1'), self.bot, self.state)
        await nodes.process_wizard_title(self.message('Private node'), self.bot, self.state)
        self.query.data = 'wizard_region:asia'
        await nodes.wizard_region_cb(self.query, self.bot, self.state)
        self.query.data = 'wizard_skip_flag'
        await nodes.wizard_skip_flag_cb(self.query, self.bot, self.state)
        self.assertEqual(await self.state.get_state(), NodeDraftState.waiting_for_target.state)
        self.assertEqual(self.draw.call_args.args[3][0][0].callback_data, 'wizard_back:flag')
        self.assertEqual((await self.state.get_data())['wizard_data']['region'], 'Asia')

    async def test_existing_numbers_include_later_pages_and_each_draft_has_fresh_identity(self):
        self.backend.request.side_effect = [
            {'items': [{'key': 'lv2'}, {'key': 'irrelevant'}], 'next_cursor': 'next-page'},
            {'items': [{'key': 'lv11-12345678'}], 'next_cursor': None}]
        await self.choose('local')
        w = (await self.state.get_data())['wizard_data']
        self.assertEqual(w['title'], 'Latvia #12')
        self.assertRegex(w['key'], r'^lv12-[a-f0-9]{8}$')
        params = parse_qs(urlsplit(self.backend.request.call_args.args[1]).query)
        self.assertEqual(params['cursor'], ['next-page'])
        other = NODE_TEMPLATES[0].draft(['lv2', 'lv11-12345678'])
        self.assertNotEqual(other['key'], w['key'])

    async def test_template_lookup_failure_does_not_advance_or_overwrite_draft(self):
        self.backend.request.side_effect = BackendError('backend_unavailable', 503)
        await self.choose('local')
        self.assertEqual(await self.state.get_state(), NodeDraftState.waiting_for_template.state)
        self.assertNotIn('key', (await self.state.get_data())['wizard_data'])
        self.assertIn(tr('en', 'node.wizard.unavailable'), self.draw.call_args.args[2].plain())

    async def test_small_catalog_is_localized_and_region_grouped(self):
        self.assertEqual(len(NODE_TEMPLATES), 6)
        for locale in ('ru', 'en'):
            await self.state.update_data(locale=locale, wizard_data={'transport': 'local'})
            await nodes.render_wizard_templates(123, self.bot, self.state, 77)
            screen, rows = self.draw.call_args.args[2:4]
            self.assertEqual(len(screen.sections), 3)
            labels = [b.text for s in screen.sections for row in s.rows for b in row]
            for template in NODE_TEMPLATES:
                self.assertIn(template.flag + ' ' + tr(locale, 'node.template.' + template.code), labels)
            self.assertEqual([b.size for b in screen.rich(rows).blocks if b.type == 'heading'], [1, 2, 2, 2])
