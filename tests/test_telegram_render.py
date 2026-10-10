"""Recover a single control screen when its old message or Rich API is unavailable."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest, TelegramNotFound, TelegramNetworkError
from aiogram.methods import EditMessageText, SendRichMessage
from aiogram import Bot
import json
from telegram_client.routers.common import render, send_notice
from telegram_client.screens import Screen, Section, Table
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from telegram_client.routers import user


class RenderRecoveryTests(IsolatedAsyncioTestCase):
    async def test_start_sends_new_panel_even_if_hidden_old_message_accepts_edits(self):
        message = SimpleNamespace(chat=SimpleNamespace(id=1, type='private'), delete=AsyncMock(),
            from_user=SimpleNamespace(id=101, username='admin', first_name='Admin',
                last_name=None, language_code='en'))
        backend = SimpleNamespace(resolve=AsyncMock(),
            me=AsyncMock(return_value={'role': 'admin', 'status': 'approved',
                'locale': 'en', 'locale_selected': True}),
            bot_title=AsyncMock(return_value={'title': 'Node Plane'}),
            admin_nodes=AsyncMock(return_value={'items': [{'key': 'lv1'}]}))
        await user.start_cmd(message, self.bot, backend, self.state)
        self.bot.edit_message_text.assert_not_awaited()
        self.bot.send_rich_message.assert_awaited_once()
        self.assertEqual(self.data['control_message_id'], 20)

    async def test_navigation_invalidates_registry_removal_confirmation_on_same_message(self):
        self.data['registry_removal_confirmation'] = {'node_key': 'node', 'message_id': 10}
        await render(self.bot, 1, Screen('Main menu'), [], self.state)
        self.assertIsNone(self.data['registry_removal_confirmation'])
        self.assertEqual(self.data['control_message_id'], 10)

    async def test_empty_collapsed_sections_do_not_emit_empty_rich_details(self):
        screen = Screen('Progress', sections=(Section('Servers', collapsed=True,
            sections=(Section('Empty region', collapsed=True),)),))
        self.assertEqual([block.type for block in screen.rich().blocks], ['heading'])
        action = InlineKeyboardButton(text='Edit', callback_data='edit')
        populated = Screen('Profile', sections=(Section('Access', collapsed=True,
            heading_rows=((action,),)),))
        self.assertEqual([block.type for block in populated.rich().blocks], ['heading', 'heading', 'buttons'])

    async def test_nested_interactive_sections_are_expanded_and_read_only_details_remain(self):
        action = InlineKeyboardButton(text='Open', callback_data='open')
        screen = Screen('Screen', sections=(Section('Servers', collapsed=True, sections=(
            Section('Region', collapsed=True, sections=(Section('Server', rows=((action,),)),)),
            Section('Help', lines=('Read only',), collapsed=True),)),))
        blocks = screen.rich().blocks
        self.assertEqual([block.type for block in blocks], ['heading', 'heading', 'heading', 'heading', 'buttons', 'details'])
        self.assertEqual(blocks[-2].buttons[0].callback_data, 'open')
        self.assertEqual(blocks[-1].summary, 'Help')

    async def test_single_destructive_action_remains_red_in_navigation_screen(self):
        cleanup = InlineKeyboardButton(text='Cleanup', callback_data='cleanup', style='danger')
        blocks = Screen('Maintenance', embedded_buttons=True, navigation=True).rich([[cleanup]]).blocks
        self.assertEqual(blocks[-1].buttons[0].style, 'danger')

    async def test_wizard_back_and_next_share_row_without_losing_primary_action_style(self):
        back = InlineKeyboardButton(text='← Back', callback_data='back')
        next_button = InlineKeyboardButton(text='Next', callback_data='next', style='primary')
        blocks = Screen('Create profile', ('Name: Alice',), embedded_buttons=True,
                        navigation=True).rich([[back, next_button]]).blocks
        self.assertEqual([block.type for block in blocks[-2:]], ['divider', 'buttons'])
        self.assertEqual([button.style for button in blocks[-1].buttons], ['link', 'primary'])

    async def test_notice_uses_embedded_buttons_and_preserves_them_in_plain_fallback(self):
        screen = Screen('Request', ('Awaiting a decision',), embedded_buttons=True)
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='Approve', callback_data='approve', style='primary'),
            InlineKeyboardButton(text='Reject', callback_data='reject', style='danger')]])
        await send_notice(self.bot, 1, screen, markup)
        args = self.bot.send_rich_message.call_args.kwargs
        self.assertEqual(args['reply_markup'].inline_keyboard, [])
        self.assertEqual([b.callback_data for b in args['rich_message'].blocks[-1].buttons],
                         ['approve', 'reject'])
        self.bot.send_rich_message.side_effect = TelegramNotFound(
            method=SendRichMessage(chat_id=1, rich_message=screen.rich()), message='Not Found')
        await send_notice(self.bot, 1, screen, markup)
        self.assertEqual(self.bot.send_message.call_args.kwargs['reply_markup'], markup)

    async def test_native_table_serialization_and_plain_fallback_preserve_labels(self):
        table = Table(('Component', 'Version', 'State'),
                      (('Agent', '1.2.3', 'Ready'), ('Runtime', '1.2.2', 'Update available')))
        screen = Screen('Administration', sections=(Section('Versions', tables=(table,)),),
                        embedded_buttons=True, navigation=True)
        back = InlineKeyboardButton(text='Back', callback_data='back')
        bot = Bot('123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi')
        try:
            payload = json.loads(bot.session.prepare_value(screen.rich([[back]]), bot=bot, files={}))
            block = next(block for block in payload['blocks'] if block['type'] == 'table')
            self.assertTrue(block['is_compact'])
            self.assertTrue(all(cell['is_header'] for cell in block['cells'][0]))
            self.assertFalse(block['cells'][1][0]['is_header'])
            self.assertEqual(block['cells'][1][1]['text'], '1.2.3')
        finally:
            await bot.session.close()
        self.bot.edit_message_text.side_effect = [TelegramBadRequest(
            method=EditMessageText(chat_id=1, message_id=10, text='old'),
            message='tables unsupported'), None]
        self.assertFalse(await render(self.bot, 1, screen, [[back]], self.state))
        arguments = self.bot.edit_message_text.call_args.kwargs
        self.assertIn('Component: Agent · Version: 1.2.3 · State: Ready', arguments['text'])
        self.assertIn('Component: Runtime · Version: 1.2.2 · State: Update available', arguments['text'])
        self.assertEqual(arguments['reply_markup'].inline_keyboard[0][0].callback_data, 'back')

    def setUp(self):
        self.data = {'control_message_id': 10}
        async def get_data():
            return dict(self.data)
        async def update_data(**values):
            self.data.update(values)
        self.state = SimpleNamespace(get_data=get_data, update_data=update_data, set_state=AsyncMock())
        self.bot = SimpleNamespace(edit_message_text=AsyncMock(),
            send_rich_message=AsyncMock(return_value=SimpleNamespace(message_id=20)),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=30)))

    async def test_embedded_actions_and_navigation_keep_same_callbacks_in_fallback(self):
        action = InlineKeyboardButton(text='AmneziaWG', callback_data='u:server-action')
        back = InlineKeyboardButton(text='Back', callback_data='u:back')
        screen = Screen('Servers', sections=(Section('Latvia', rows=((action,),)),),
                        embedded_buttons=True, navigation=True)
        self.assertTrue(await render(self.bot, 1, screen, [[back]], self.state))
        arguments = self.bot.edit_message_text.call_args.kwargs
        self.assertEqual(arguments['reply_markup'].inline_keyboard, [])
        blocks = arguments['rich_message'].blocks
        self.assertEqual(blocks[2].buttons[0].callback_data, action.callback_data)
        self.assertEqual(blocks[-1].buttons[0].callback_data, back.callback_data)
        self.assertEqual(blocks[-1].buttons[0].style, 'link')
        self.assertEqual(blocks[-2].type, 'divider')
        self.bot.edit_message_text.side_effect = [TelegramBadRequest(
            method=EditMessageText(chat_id=1, message_id=10, text='old'),
            message='rich buttons unsupported'), None]
        self.assertFalse(await render(self.bot, 1, screen, [[back]], self.state))
        rows = self.bot.edit_message_text.call_args.kwargs['reply_markup'].inline_keyboard
        self.assertEqual([button.callback_data for row in rows for button in row],
                         [action.callback_data, back.callback_data])

    async def test_inline_back_is_also_separated_from_screen_content(self):
        screen = Screen('Help', ('Instructions',))
        for title in ('← Back', '← Назад'):
            with self.subTest(title=title):
                rows = [[InlineKeyboardButton(text=title, callback_data='back')]]
                self.assertEqual(screen.rich(rows).blocks[-1].type, 'divider')

    async def test_embedded_config_media_serializes_as_multipart_uploads(self):
        bot = Bot('123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi')
        try:
            screen = Screen('Config', uri='vpn://fresh', qr=b'PNG', qr_title='QR',
                files=(('node.vpn', b'vpn://fresh'), ('node.conf', b'[Interface]')),
                files_title='Configuration files')
            files = {}
            payload = json.loads(bot.session.prepare_value(screen.rich(), bot=bot, files=files))
            self.assertEqual(len(files), 3)
            self.assertFalse(payload['blocks'][1]['is_open'])
            self.assertTrue(payload['blocks'][1]['blocks'][0]['photo']['media'].startswith('attach://'))
            self.assertEqual(payload['blocks'][2]['type'], 'paragraph')
            self.assertEqual(payload['blocks'][2]['text'], {'type': 'code', 'text': 'vpn://fresh'})
            self.assertEqual(payload['blocks'][3]['type'], 'heading')
            self.assertEqual([block['type'] for block in payload['blocks'][4:]],
                             ['document', 'document'])
            self.assertEqual({file.filename for file in files.values()},
                             {'config.png', 'node.vpn', 'node.conf'})
        finally:
            await bot.session.close()

    async def test_plain_fallback_keeps_uri_monospace_with_utf16_offsets(self):
        screen = Screen('🔑 Конфиг', uri='vless://test')
        self.bot.edit_message_text.side_effect = [TelegramBadRequest(
            method=EditMessageText(chat_id=1, message_id=10, text='old'),
            message='unsupported rich message'), None]
        self.assertFalse(await render(self.bot, 1, screen, [], self.state))
        arguments = self.bot.edit_message_text.call_args.kwargs
        entity = arguments['entities'][0]
        self.assertEqual(entity.type, 'code')
        encoded = arguments['text'].encode('utf-16-le')
        self.assertEqual(encoded[entity.offset * 2:(entity.offset + entity.length) * 2].decode('utf-16-le'),
                         'vless://test')

    async def test_deleted_screen_is_recreated_for_both_telegram_error_codes(self):
        for error in (TelegramBadRequest, TelegramNotFound):
            with self.subTest(error=error):
                self.bot.edit_message_text.side_effect = error(
                    method=EditMessageText(chat_id=1, message_id=10, text='old'),
                    message='message to edit not found')
                await render(self.bot, 1, Screen('ID', ('101',)), [], self.state)
                self.assertEqual(self.data['control_message_id'], 20)

    async def test_unsupported_rich_send_falls_back_to_plain_and_records_new_id(self):
        self.data.clear()
        self.bot.send_rich_message.side_effect = TelegramNotFound(
            method=SendRichMessage(chat_id=1, rich_message=Screen('ID').rich()), message='Not Found')
        await render(self.bot, 1, Screen('ID', ('101',)), [], self.state)
        self.assertEqual(self.data['control_message_id'], 30)
        self.assertEqual(self.bot.send_message.call_args.kwargs['text'], 'ID\n\n101')

    async def test_rich_network_timeout_does_not_leave_command_without_a_screen(self):
        self.data.clear()
        self.bot.send_rich_message.side_effect = TelegramNetworkError(
            method=SendRichMessage(chat_id=1, rich_message=Screen('Version').rich()), message='timeout')
        await render(self.bot, 1, Screen('Version'), [], self.state)
        self.assertEqual(self.bot.send_rich_message.call_args.kwargs['request_timeout'], 10)
        self.assertEqual(self.data['control_message_id'], 30)

    async def test_unchanged_message_does_not_send_a_duplicate(self):
        self.bot.edit_message_text.side_effect = TelegramBadRequest(
            method=EditMessageText(chat_id=1, message_id=10, text='old'), message='message is not modified')
        await render(self.bot, 1, Screen('ID'), [], self.state)
        self.bot.send_rich_message.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()

    async def test_edit_fallback_is_logged_without_content_and_next_screen_retries_rich(self):
        self.bot.edit_message_text.side_effect = [TelegramNetworkError(
            method=EditMessageText(chat_id=1, message_id=10, text='private'),
            message='timeout https://api.telegram.org/botSECRET'), None, None]
        back = InlineKeyboardButton(text='Back', callback_data='back')
        with self.assertLogs('telegram_client.routers.common', level='WARNING') as logs:
            self.assertFalse(await render(self.bot, 1,
                Screen('Private profile', uri='vless://SECRET', embedded_buttons=True), [[back]], self.state))
        output = '\n'.join(logs.output)
        self.assertIn('stage=edit', output)
        self.assertIn('reason=network_timeout', output)
        self.assertNotIn('SECRET', output)
        self.assertNotIn('Private profile', output)
        self.assertTrue(await render(self.bot, 1, Screen('Menu', embedded_buttons=True), [[back]], self.state))
        self.assertIn('rich_message', self.bot.edit_message_text.call_args.kwargs)
        self.assertEqual(self.data['control_message_id'], 10)
        self.bot.send_message.assert_not_awaited()

    async def test_commands_recreate_deleted_control_screen(self):
        self.bot.delete_message = AsyncMock()
        self.bot.edit_message_text.side_effect = TelegramBadRequest(
            method=EditMessageText(chat_id=1, message_id=10, text='old'), message='message to edit not found')
        message = SimpleNamespace(chat=SimpleNamespace(id=1, type='private'), delete=AsyncMock(),
            from_user=SimpleNamespace(id=101, username='admin', first_name='Admin',
                last_name=None, language_code='en'))
        backend = SimpleNamespace(resolve=AsyncMock(),
            me=AsyncMock(return_value={'role': 'admin', 'status': 'approved', 'locale': 'en', 'locale_selected': True}),
            bot_title=AsyncMock(return_value={'title': 'Node Plane'}),
            system_version=AsyncMock(return_value={'version': '0.4.3-alpha.22'}))
        for command in (user.start_cmd, user.id_cmd, user.version_cmd):
            with self.subTest(command=command.__name__):
                self.data['control_message_id'] = 10
                before = self.bot.send_rich_message.await_count
                await command(message, self.bot, backend, self.state)
                self.assertEqual(self.bot.send_rich_message.await_count, before + 1)
                self.assertEqual(self.data['control_message_id'], 20)

    async def test_heading_hierarchy_and_compact_unheaded_lists(self):
        server = Section('Latvia #1', ('Traffic: 1 GiB',))
        region = Section('Latvia', sections=(server,))
        screen = Screen('Servers', sections=(region,))
        headings = [b for b in screen.rich().blocks if b.type == 'heading']
        self.assertEqual([(b.text, b.size) for b in headings],
                         [('Servers', 1), ('Latvia', 2), ('Latvia #1', 3)])
        collapsed = Screen('Profile', sections=(Section('Servers', collapsed=True,
            sections=(region,)),))
        details = collapsed.rich().blocks[1]
        self.assertEqual([(b.text, b.size) for b in details.blocks if b.type == 'heading'],
                         [('Latvia', 2), ('Latvia #1', 3)])
        compact = Screen('Profiles', sections=(Section('', rows=((
            InlineKeyboardButton(text='Alice · Active', callback_data='alice'),),)),))
        blocks = compact.rich().blocks
        self.assertEqual([b.type for b in blocks], ['heading', 'buttons'])
        self.assertEqual(blocks[1].buttons[0].text, 'Alice · Active')
