"""Guided entry keeps Back/breadcrumbs scoped to Quick Start."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from telegram_client.navigation import parent_path
from telegram_client.routers import quick_start, admin_nodes, admin_profiles, user


class QuickStartContextTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=123, user_id=123))
        self.query = SimpleNamespace(data='', answer=AsyncMock(),
            message=SimpleNamespace(chat=SimpleNamespace(id=123), message_id=77),
            from_user=SimpleNamespace(id=123))
        self.bot = SimpleNamespace()
        home = user.button(123, 'Menu', 'home').callback_data
        await self.state.update_data(locale='en', quick_start_home=home)

    async def test_server_creation_and_protocol_entry_retain_guide_context(self):
        for step, handler in [('node', 'new_node_cb'), ('protocols', 'admin_node_cb')]:
            self.query.data = 'quick_start_open:' + step
            backend = SimpleNamespace(request=AsyncMock(return_value={'next_step':step, 'node_key':'lv1'}))
            with patch.object(admin_nodes, handler, new_callable=AsyncMock) as opened:
                await quick_start.open_step(self.query, self.bot, backend, self.state)
            opened.assert_awaited_once()
            await admin_nodes._clear_node_flow(self.state)
            data = await self.state.get_data()
            self.assertEqual(data['quick_start_context']['step'], step)
            callbacks = [p.callback for p in parent_path('node_settings:lv1', 'en', data)]
            self.assertIn('quick_start', callbacks)
            self.assertNotIn('admin_menu', callbacks)

    async def test_access_entry_and_clear_preserve_guide_return(self):
        self.query.data = 'quick_start_open:access'
        backend = SimpleNamespace(request=AsyncMock(return_value={'next_step':'access'}))
        with patch.object(admin_profiles, 'show_admin_profiles', new_callable=AsyncMock):
            await quick_start.open_step(self.query, self.bot, backend, self.state)
        await admin_profiles._clear_flow(self.state)
        data = await self.state.get_data()
        self.assertIn('quick_start', [p.callback for p in parent_path('admin_profiles', 'en', data)])

    async def test_config_list_returns_to_guide_and_normal_list_returns_home(self):
        backend = SimpleNamespace(profiles=AsyncMock(return_value={'items':[
            {'id':'p1','display_name':'One'}, {'id':'p2','display_name':'Two'}]}))
        for context, expected in [({'step':'config'}, 'quick_start'), (None, 'home')]:
            await self.state.update_data(quick_start_context=context)
            with patch.object(user, 'render', new_callable=AsyncMock) as draw:
                await user.show_profiles(123, 123, 77, self.bot, backend, self.state)
            callback = draw.call_args.args[3][-1][0].callback_data
            self.assertEqual(user.actions[callback[2:]].name, expected)
            if context:
                path = parent_path(user.button(123,'Config','node','p1','lv1').callback_data,
                    'en', await self.state.get_data())
                self.assertIn('quick_start', [p.callback for p in path])

    async def test_quick_start_action_labels_match_callbacks(self):
        cases = [
            # (step, reviewed, expected_primary_text, expected_primary_cb, expected_secondary_text, expected_secondary_cb)
            ('defaults', False, 'Configure installation defaults', 'idefault:guide', 'Use installation defaults', 'quick_start_defaults'),
            ('defaults', True, 'Use installation defaults', 'quick_start_defaults', 'Configure installation defaults', 'idefault:guide'),
            ('node', False, 'Add server', 'quick_start_open:node', None, None),
            ('protocols', False, 'Set up server', 'quick_start_open:protocols', None, None),
            ('access', False, 'Configure access', 'quick_start_open:access', None, None),
            ('config', False, 'Get config', 'quick_start_open:config', None, None),
        ]
        for step, reviewed, p_text, p_cb, s_text, s_cb in cases:
            await self.state.update_data(quick_start_defaults_reviewed=reviewed)
            steps_info = [{'key': s, 'complete': False} for s in ('defaults', 'node', 'protocols', 'access', 'config')]
            backend = SimpleNamespace(request=AsyncMock(return_value={
                'completed': False, 'steps': steps_info, 'next_step': step
            }))
            with patch.object(quick_start, 'render', new_callable=AsyncMock) as draw:
                await quick_start.show(123, 123, 77, self.bot, backend, self.state)
            rows = draw.call_args.args[3]
            # Primary button is the first row
            self.assertEqual(rows[0][0].text, p_text)
            self.assertEqual(rows[0][0].callback_data, p_cb)
            if s_cb:
                self.assertEqual(rows[1][0].text, s_text)
                self.assertEqual(rows[1][0].callback_data, s_cb)
