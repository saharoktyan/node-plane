"""Structural navigation, long paths and returning without replaying operations."""
import json
from types import SimpleNamespace
from unittest import TestCase, IsolatedAsyncioTestCase
from unittest.mock import AsyncMock
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, RichTextButton, RichTextBold, CallbackQuery, User, Message, Chat
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from telegram_client.screens import Screen, Breadcrumb, breadcrumb_text, text_width
from telegram_client.navigation import parent_path, screen_parents
from telegram_client.routers.common import render
from telegram_client.routers.navigation import navigation_cb, NavigationGuardMiddleware
from telegram_client.routers.admin_nodes import _clear_node_flow


class NavigationPathTests(TestCase):
    def test_member_paths_keep_owner_binding_and_never_issue_configurations(self):
        from telegram_client.routers import user
        owner = 123
        callback = user.button(owner, 'Back', 'device_picker', 'p1', 'lv1').callback_data
        path = parent_path(callback, 'en', {'_navigation_nodes': {'lv1': 'Latvia #1'}})
        actions = [user.actions[p.callback[2:]] for p in path]
        self.assertEqual([a.name for a in actions], ['home', 'profile', 'node', 'protocol', 'device_picker'])
        self.assertTrue(all(a.owner_id == owner for a in actions))
        self.assertEqual(path[2].label, 'Latvia #1')
        for name, args in (('issue', ('p1','lv1','awg','')),
                           ('device_delete_confirm', ('p1','d1')), ('device_pick', ('p1','lv1','d1'))):
            unsafe = user.button(owner, 'Action', name, *args).callback_data
            self.assertEqual(parent_path(unsafe, 'en', {}), ())

    def test_known_destinations_have_translations_and_real_root_callbacks(self):
        from telegram_client.routers.callbacks import AdminNodesCallback, AdminSettingsCallback, AdminProfilesCallback
        for locale in ('en', 'ru'):
            callbacks = ('admin_menu', AdminNodesCallback().pack(), AdminProfilesCallback().pack(),
                AdminSettingsCallback().pack(), 'updates', 'backups', 'backup_settings', 'alerts',
                'traffic', 'bot_title_settings', 'request_policy', 'recpage:0', 'recpage:10',
                'wsaudit:0', 'system_cleanup', 'idefault:open', 'ssh_key', 'announce_menu',
                'admin_status', 'uv_page:0', 'ufleet', 'backup_list:0', 'request_page:0',
                'prof_role:p1', 'prof_expiry:p1', 'node_section:connection:lv1')
            for callback in callbacks:
                with self.subTest(locale=locale, callback=callback):
                    path = parent_path(callback, locale, {})
                    self.assertTrue(path)
                    self.assertFalse(any('.' in p.label for p in path))

    def test_server_hierarchy_is_structural_and_language_aware(self):
        for locale in ('en', 'ru'):
            data = {'_navigation_nodes': {'lv1': '🇱🇻 Latvia #1'}}
            path = parent_path('node_section:awg:lv1', locale, data)
            self.assertEqual([p.callback for p in path],
                ['admin_menu', 'admin_nodes', 'admin_node:lv1', 'node_settings:lv1', 'node_section:awg:lv1'])
            self.assertEqual(path[2].label, '🇱🇻 Latvia #1')
            self.assertFalse(any('.' in p.label for p in path))

    def test_only_navigation_callbacks_are_admitted(self):
        for callback in ('node_action:bootstrap:lv1', 'node_job_submit', 'remove_retry:lv1',
                         'retire_registry:lv1', 'upd_act:latest', 'traffic:on', 'idefault:save'):
            self.assertEqual(parent_path(callback, 'en', {}), ())
            rows = [[InlineKeyboardButton(text='Start', callback_data=callback, style='danger')]]
            self.assertEqual(screen_parents(Screen('Confirm', navigation=True), rows, 'en', {}), ())

    def test_back_in_confirmation_row_is_used_without_including_confirm(self):
        rows = [[InlineKeyboardButton(text='← Back', callback_data='bootstrap_menu:lv1'),
                 InlineKeyboardButton(text='Continue', callback_data='node_job_submit', style='primary')]]
        path = screen_parents(Screen('Bootstrap', navigation=True), rows, 'en', {})
        self.assertEqual(path[-1].callback, 'bootstrap_menu:lv1')
        self.assertNotIn('node_job_submit', [p.callback for p in path])

    def test_short_path_keeps_every_parent_and_current_screen_is_not_button(self):
        path = (Breadcrumb('Admin', 'admin_menu'), Breadcrumb('Servers', 'admin_nodes'))
        text = breadcrumb_text(path, 'Latvia #1', 'nav_more:nonce')
        self.assertEqual([p.button.text for p in text if isinstance(p, RichTextButton)], ['Admin', 'Servers'])
        self.assertIsInstance(text[-1], RichTextBold)
        self.assertEqual(text[-1].text, 'Latvia #1')

    def test_long_path_keeps_last_parent_and_hides_left_levels_without_initials(self):
        path = tuple(Breadcrumb(label, 'route:' + str(i)) for i, label in enumerate(
            ('Admin', 'Servers', 'Latvia #1', 'Advanced settings', 'AmneziaWG')))
        text = breadcrumb_text(path, 'Connection settings', 'nav_more:nonce')
        buttons = [p.button for p in text if isinstance(p, RichTextButton)]
        self.assertEqual([b.text for b in buttons], ['…', 'AmneziaWG'])
        self.assertEqual(buttons[0].callback_data, 'nav_more:nonce')
        self.assertEqual(buttons[1].callback_data, 'route:4')

    def test_wide_unicode_and_long_name_preserve_full_heading(self):
        name = 'Очень длинное название сервера 🇱🇻 ' * 3
        screen = Screen(name, breadcrumbs=(Breadcrumb('Admin', 'admin_menu'),), breadcrumb_more='nav_more:n')
        blocks = screen.rich().blocks
        self.assertEqual(blocks[1].text, name)
        self.assertEqual(blocks[1].size, 1)
        self.assertGreater(text_width('日本'), len('日本'))
        self.assertIn(name, screen.plain())

    def test_file_card_keeps_full_path_without_snapshot_or_hidden_destinations(self):
        path = tuple(Breadcrumb('Very long ancestor name ' + str(i), 'route:' + str(i)) for i in range(4))
        text = breadcrumb_text(path, 'SSH key')
        self.assertEqual([p.button.callback_data for p in text if isinstance(p, RichTextButton)],
                         [p.callback for p in path])


class NavigationRenderTests(IsolatedAsyncioTestCase):
    async def test_config_media_path_shortens_without_storing_secrets_and_returns_by_lookup(self):
        from telegram_client.routers import user
        parents = tuple(Breadcrumb(label, user.button(123, label, name, *args).callback_data)
            for label, name, args in [('Menu', 'home', ()), ('Get config', 'profile', ('p1',)),
                ('Petersburg #1', 'node', ('p1', 'lv1')), ('AmneziaWG', 'protocol', ('p1','lv1','awg')),
                ('Devices', 'device_picker', ('p1','lv1'))])
        lookup = user.button(123, 'Config', 'issuance', 'existing').callback_data
        screen = Screen('My phone', uri='vpn://secret', uri_collapsed=True,
            qr=b'private-qr', qr_title='QR', files=(('secret.conf', b'private-config'),),
            breadcrumbs=parents, navigation_return=lookup, embedded_buttons=True, navigation=True)
        await render(self.bot, 123, screen, [[InlineKeyboardButton(text='Back', callback_data=parents[-1].callback)]], self.state)
        rich = self.bot.edit_message_text.call_args.kwargs['rich_message']
        first = next(p for p in rich.blocks[0].text if isinstance(p, RichTextButton))
        self.assertEqual(first.button.text, '…')
        snapshot = self.data['navigation_screen']
        self.assertNotIn('rich', snapshot)
        self.assertNotIn('secret', json.dumps(snapshot))
        self.query.data = 'nav_more:' + snapshot['nonce']
        await navigation_cb(self.query, self.bot, self.state)
        dispatcher = SimpleNamespace(propagate_event=AsyncMock())
        query = CallbackQuery(id='return', from_user=User(id=123, is_bot=False, first_name='User'),
            chat_instance='test', data='nav_return:' + snapshot['nonce'],
            message=Message(message_id=77, date=0, chat=Chat(id=123, type='private')))
        backend = object()
        await navigation_cb(query, self.bot, self.state, dispatcher, backend=backend)
        forwarded = dispatcher.propagate_event.call_args.kwargs
        self.assertEqual(forwarded['event'].data, lookup)
        self.assertIs(forwarded['backend'], backend)
        self.assertEqual(user.actions[lookup[2:]].name, 'issuance')

    async def test_discard_reaches_real_router_with_same_backend_and_fsm(self):
        from aiogram import Dispatcher, Router, F
        await self.draw()
        nonce = self.data['navigation_screen']['nonce']
        self.data.update(edit_profile_id='p1', draft_grants=[{'node_key': 'lv1'}], original_grants=[],
            navigation_discard={'nonce': nonce, 'callback': 'admin_menu', 'kind': 'access'})
        query = CallbackQuery(id='test', from_user=User(id=123, is_bot=False, first_name='Admin'),
            chat_instance='test', data='nav_discard:' + nonce,
            message=Message(message_id=77, date=0, chat=Chat(id=123, type='private')))
        dispatcher, router = Dispatcher(), Router()
        backend, visited = object(), []
        @router.callback_query(F.data == 'admin_menu')
        async def destination(query, state, backend):
            visited.append((query.data, state, backend))
        dispatcher.include_router(router)
        try:
            await navigation_cb(query, self.bot, self.state, dispatcher, backend=backend)
            self.assertEqual(visited, [('admin_menu', self.state, backend)])
        finally:
            await dispatcher.storage.close()

    async def test_dirty_access_requires_confirmation_before_parent_handler(self):
        await self.draw()
        self.data.update(edit_profile_id='p1', draft_grants=[{'node_key': 'lv1'}], original_grants=[])
        self.query.data = 'admin_menu'
        handler = AsyncMock()
        middleware = NavigationGuardMiddleware()
        event = SimpleNamespace(callback_query=self.query)
        await middleware(handler, event, {'state': self.state, 'bot': self.bot})
        handler.assert_not_awaited()
        rich = self.bot.edit_message_text.call_args.kwargs['rich_message']
        self.assertEqual(rich.blocks[-1].buttons[-1].style, 'danger')
        self.assertEqual(self.data['draft_grants'], [{'node_key': 'lv1'}])
        self.query.data = 'nav_return:' + self.data['navigation_screen']['nonce']
        await navigation_cb(self.query, self.bot, self.state)
        self.assertEqual(self.data['draft_grants'], [{'node_key': 'lv1'}])
        self.assertIsNone(self.data['navigation_discard'])

    async def test_confirmed_discard_dispatches_only_selected_parent(self):
        await self.draw()
        nonce = self.data['navigation_screen']['nonce']
        self.data.update(edit_profile_id='p1', draft_grants=[{'node_key': 'lv1'}], original_grants=[],
            navigation_discard={'nonce': nonce, 'callback': 'admin_menu', 'kind': 'access'})
        query = CallbackQuery(id='test', from_user=User(id=123, is_bot=False, first_name='Admin'),
            chat_instance='test', data='nav_discard:' + nonce,
            message=Message(message_id=77, date=0, chat=Chat(id=123, type='private')))
        dispatcher = SimpleNamespace(propagate_event=AsyncMock())
        await navigation_cb(query, self.bot, self.state, dispatcher)
        redirected = dispatcher.propagate_event.call_args.kwargs
        self.assertEqual(redirected['event'].data, 'admin_menu')
        self.assertEqual(redirected['event'].from_user.id, 123)
        self.assertIs(redirected['state'], self.state)
        self.assertIsNone(self.data['edit_profile_id'])
        self.assertIsNone(self.data['draft_grants'])

    async def test_node_settings_draft_is_preserved_without_an_exit_prompt(self):
        await self.draw()
        self.query.data = 'admin_nodes'
        handler = AsyncMock()
        await NavigationGuardMiddleware()(handler, SimpleNamespace(callback_query=self.query),
                                           {'state': self.state, 'bot': self.bot})
        handler.assert_awaited_once()
        self.assertEqual(self.data['node_settings_draft']['values']['title'], 'New name')

    async def test_abandoning_server_creation_requires_confirmation(self):
        await self.draw()
        self.data.update(wizard_data={'title': 'New node'}, wizard_saved=False)
        self.query.data = 'admin_nodes'
        handler = AsyncMock()
        await NavigationGuardMiddleware()(handler, SimpleNamespace(callback_query=self.query),
                                           {'state': self.state, 'bot': self.bot})
        handler.assert_not_awaited()
        self.assertEqual(self.data['wizard_data']['title'], 'New node')
        self.assertEqual(self.data['navigation_discard']['kind'], 'node')

    def setUp(self):
        self.data = {'control_message_id': 77, 'locale': 'en',
            'admin_node_search': 'Latvia', 'admin_node_page': 2,
            '_navigation_nodes': {'lv1': 'Latvia #1'},
            'node_settings_draft': {'node_key': 'lv1', 'values': {'title': 'New name'}}}
        async def get_data(): return dict(self.data)
        async def update_data(**values): self.data.update(values)
        async def clear(): self.data.clear()
        self.state = SimpleNamespace(get_data=get_data, update_data=update_data, clear=clear,
                                     get_state=AsyncMock(return_value=None))
        self.bot = SimpleNamespace(edit_message_text=AsyncMock())
        self.query = SimpleNamespace(data='', answer=AsyncMock(),
            message=SimpleNamespace(message_id=77, chat=SimpleNamespace(id=123)))

    async def draw(self):
        screen = Screen('Bootstrap', ('Choose installation steps.',), embedded_buttons=True, navigation=True)
        rows = [[InlineKeyboardButton(text='← Back', callback_data='node_settings:lv1')]]
        await render(self.bot, 123, screen, rows, self.state)
        return self.bot.edit_message_text.call_args.kwargs['rich_message']

    async def test_native_inline_buttons_serialize_without_markup_or_url(self):
        rich = await self.draw()
        bot = Bot('123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi')
        try:
            payload = json.loads(bot.session.prepare_value(rich, bot=bot, files={}))
        finally:
            await bot.session.close()
        self.assertEqual(payload['blocks'][0]['type'], 'paragraph')
        buttons = [part['button'] for part in payload['blocks'][0]['text'] if isinstance(part, dict) and part['type'] == 'button']
        self.assertTrue(buttons)
        self.assertTrue(all(b['style'] == 'link' and b['callback_data'] for b in buttons))
        self.assertEqual([block.type for block in rich.blocks[-2:]], ['divider', 'buttons'])

    async def test_expand_and_return_reuse_message_without_changing_draft_or_page(self):
        original = await self.draw()
        snapshot = self.data['navigation_screen']
        self.query.data = 'nav_more:' + snapshot['nonce']
        await navigation_cb(self.query, self.bot, self.state)
        menu = self.bot.edit_message_text.call_args.kwargs['rich_message']
        callbacks = [b.callback_data for block in menu.blocks if block.type == 'buttons' for b in block.buttons]
        self.assertEqual(callbacks[:-1], [p['callback'] for p in snapshot['parents']])
        self.query.data = 'nav_return:' + snapshot['nonce']
        await navigation_cb(self.query, self.bot, self.state)
        restored = self.bot.edit_message_text.call_args.kwargs['rich_message']
        self.assertEqual(original.model_dump(), restored.model_dump())
        self.assertEqual(self.data['admin_node_page'], 2)
        self.assertEqual(self.data['admin_node_search'], 'Latvia')
        self.assertEqual(self.data['node_settings_draft']['values']['title'], 'New name')
        self.assertEqual(self.data['control_message_id'], 77)

    async def test_stale_callback_and_other_panel_cannot_restore_old_screen(self):
        await self.draw()
        nonce = self.data['navigation_screen']['nonce']
        for token, message in (('old', 77), (nonce, 99)):
            self.query.data = 'nav_more:' + token
            self.query.message.message_id = message
            before = self.bot.edit_message_text.await_count
            await navigation_cb(self.query, self.bot, self.state)
            self.assertEqual(self.bot.edit_message_text.await_count, before)
        self.query.message.message_id = 77
        await render(self.bot, 123, Screen('Home'), [], self.state)
        self.query.data = 'nav_return:' + nonce
        before = self.bot.edit_message_text.await_count
        await navigation_cb(self.query, self.bot, self.state)
        self.assertEqual(self.bot.edit_message_text.await_count, before)

    async def test_rich_rejection_keeps_navigation_in_inline_fallback(self):
        self.bot.edit_message_text.side_effect = [TelegramBadRequest(
            method=EditMessageText(chat_id=123, message_id=77, text='old'),
            message='unsupported rich content'), None]
        await self.draw_fallback()
        markup = self.bot.edit_message_text.call_args.kwargs['reply_markup']
        self.assertEqual([b.callback_data for b in markup.inline_keyboard[0]], ['admin_menu', 'admin_nodes', 'admin_node:lv1'])

    async def draw_fallback(self):
        await render(self.bot, 123, Screen('Settings', embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text='← Back', callback_data='admin_node:lv1')]], self.state)

    async def test_return_fallback_keeps_all_original_actions(self):
        await self.draw()
        self.query.data = 'nav_return:' + self.data['navigation_screen']['nonce']
        self.bot.edit_message_text.side_effect = [TelegramBadRequest(
            method=EditMessageText(chat_id=123, message_id=77, text='old'), message='unsupported'), None]
        await navigation_cb(self.query, self.bot, self.state)
        markup = self.bot.edit_message_text.call_args.kwargs['reply_markup']
        self.assertEqual(markup.inline_keyboard[-1][0].callback_data, 'node_settings:lv1')

    async def test_server_list_transition_preserves_labels_search_and_draft(self):
        await _clear_node_flow(self.state)
        self.assertEqual(self.data['_navigation_nodes']['lv1'], 'Latvia #1')
        self.assertEqual(self.data['admin_node_page'], 2)
        self.assertEqual(self.data['node_settings_draft']['values']['title'], 'New name')
