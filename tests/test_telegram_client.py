"""Exercise the Telegram paths that connect a person, node, and config."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch
from uuid import UUID

from aiogram import Dispatcher

from telegram_client import main
from telegram_client.backend import BackendError, BackendClient
from telegram_client.routers import (user, admin_requests, admin_profiles,
                                     admin_nodes, admin_settings)
from telegram_client.routers.callbacks import ProbeNodeCallback
from telegram_client.routers.callbacks import NewProfileCallback


class RouterStartupTests(TestCase):
    def test_all_routers_import_and_register(self):
        dispatcher = Dispatcher()
        for module in (user, admin_requests, admin_profiles, admin_nodes,
                       admin_settings):
            dispatcher.include_router(module.router)
        self.assertEqual(len(dispatcher.sub_routers), 5)
        self.assertTrue(callable(main.main))
        self.assertIn(admin_nodes.admin_node_cb,
            [handler.callback for handler in admin_nodes.router.callback_query.handlers])


class TelegramFlowTests(IsolatedAsyncioTestCase):
    async def test_get_config_back_uses_home_presentation_without_backend_reads(self):
        backend = SimpleNamespace(
            me=AsyncMock(return_value={'role': 'member', 'status': 'approved'}),
            bot_title=AsyncMock(return_value={'title': 'Node Plane'}),
            profiles=AsyncMock(return_value={'items': []}))
        with patch.object(user, 'render', new_callable=AsyncMock) as draw:
            await user.show_home(123, 123, self.bot, backend, self.state, 77)
            backend.me.reset_mock()
            backend.bot_title.reset_mock()
            self.query.data = user.button(123, 'Back', 'home').callback_data
            await user.user_action_cb(self.query, self.bot, backend, self.state)
            self.assertEqual(draw.call_args.args[2].title, 'Node Plane')
            backend.me.assert_not_awaited()
            backend.bot_title.assert_not_awaited()
            # Menu presentation never supplies profile/access data.
            self.query.data = user.button(123, 'Get config', 'profiles').callback_data
            await user.user_action_cb(self.query, self.bot, backend, self.state)
            backend.profiles.assert_awaited_once_with(123)

    async def test_cached_home_is_not_reused_for_another_user_or_pending_account(self):
        backend = SimpleNamespace(
            me=AsyncMock(return_value={'role': 'member', 'status': 'approved'}),
            bot_title=AsyncMock(return_value={'title': 'Fresh title'}))
        for owner, status in ((999, 'approved'), (123, 'pending')):
            self.state_data['home_presentation'] = {'user_id': owner,
                'account': {'role': 'admin', 'status': status},
                'title': {'title': 'Cached title'}}
            with patch.object(user, 'render', new_callable=AsyncMock) as draw:
                await user.show_home(123, 123, self.bot, backend, self.state, 77,
                                     cached_navigation=True)
            self.assertEqual(draw.call_args.args[2].title, 'Fresh title')
        self.assertEqual(backend.me.await_count, 2)

    async def test_normal_home_refresh_ignores_cached_presentation(self):
        self.state_data['home_presentation'] = {'user_id': 123,
            'account': {'role': 'admin', 'status': 'approved'},
            'title': {'title': 'Old title'}}
        backend = SimpleNamespace(
            me=AsyncMock(return_value={'role': 'member', 'status': 'pending'}),
            bot_title=AsyncMock(return_value={'title': 'Fresh title'}),
            access_request_policy=AsyncMock(return_value={'enabled': True}),
            request=AsyncMock(return_value={'items': []}))
        with patch.object(user, 'render', new_callable=AsyncMock) as draw:
            await user.show_home(123, 123, self.bot, backend, self.state, 77)
        self.assertEqual(draw.call_args.args[2].title, 'Fresh title')
        self.assertEqual(self.state_data['home_presentation']['account']['status'], 'pending')

    async def test_idle_member_navigation_is_reusable_without_start(self):
        self.query.data = user.button(123, 'Home', 'home').callback_data
        with patch.object(user, 'time', SimpleNamespace(monotonic=lambda: 10**12)), \
             patch.object(user, 'show_home', new_callable=AsyncMock) as home:
            await user.user_action_cb(self.query, self.bot, SimpleNamespace(), self.state)
            await user.user_action_cb(self.query, self.bot, SimpleNamespace(), self.state)
        self.assertEqual(home.await_count, 2)
        self.assertIn(self.query.data[2:], user.actions)

    async def test_unknown_callback_recovers_home_after_restart_without_replaying(self):
        self.query.data = 'u:lost-on-restart'
        backend = SimpleNamespace(me=AsyncMock(return_value={
            'locale': 'ru', 'locale_selected': True, 'status': 'approved', 'role': 'member'}))
        with patch.object(user, 'show_home', new_callable=AsyncMock) as home:
            await user.user_action_cb(self.query, self.bot, backend, self.state)
        self.assertEqual(self.state_data['locale'], 'ru')
        self.assertEqual(home.call_args.args[-1], 77)
        backend.me.assert_awaited_once_with(123)
        self.query.answer.assert_awaited_once_with(text=None, request_timeout=3)

    async def test_unknown_callback_recovers_language_picker_for_new_user(self):
        self.query.data = 'u:lost-language-button'
        backend = SimpleNamespace(me=AsyncMock(return_value={'locale_selected': False, 'locale': 'en'}))
        with patch.object(user, 'show_language_picker', new_callable=AsyncMock) as picker:
            await user.user_action_cb(self.query, self.bot, backend, self.state)
        self.assertEqual(picker.call_args.args[-1], 77)

    async def test_other_user_cannot_replay_navigation(self):
        self.query.from_user.language_code = 'en'
        self.query.data = user.button(999, 'Profile', 'account_profile', 'private', 'home').callback_data
        backend = SimpleNamespace(me=AsyncMock())
        with patch.object(user, 'show_account_profile', new_callable=AsyncMock) as profile:
            await user.user_action_cb(self.query, self.bot, backend, self.state)
        profile.assert_not_awaited()
        backend.me.assert_not_awaited()
        self.assertTrue(self.query.answer.call_args.kwargs['show_alert'])

    async def test_callback_acknowledgement_failure_does_not_block_navigation(self):
        from aiogram.exceptions import TelegramNetworkError
        from aiogram.methods import AnswerCallbackQuery
        self.query.data = user.button(123, 'Home', 'home').callback_data
        self.query.answer.side_effect = TelegramNetworkError(
            method=AnswerCallbackQuery(callback_query_id='query'), message='timeout')
        with patch.object(user, 'show_home', new_callable=AsyncMock) as home:
            await user.user_action_cb(self.query, self.bot, SimpleNamespace(), self.state)
        home.assert_awaited_once()

    async def test_language_selection_reuses_saved_account_and_opens_rich_access_gate(self):
        account = {'role': 'member', 'status': 'pending', 'locale': 'ru', 'locale_selected': True}
        backend = SimpleNamespace(set_locale=AsyncMock(return_value=account), me=AsyncMock(),
            bot_title=AsyncMock(return_value={'title': 'Node Plane'}),
            access_request_policy=AsyncMock(return_value={'enabled': True}),
            request=AsyncMock(return_value={'items': []}))
        self.query.data = user.button(123, 'Русский', 'first_locale', 'ru').callback_data
        with patch.object(user, 'render', new_callable=AsyncMock) as draw:
            await user.user_action_cb(self.query, self.bot, backend, self.state)
        backend.me.assert_not_awaited()
        backend.set_locale.assert_awaited_once_with(123, 'ru')
        self.query.answer.assert_awaited_once_with(text=user.tr('ru', 'language.saving'), request_timeout=3)
        screen, rows = draw.call_args.args[2:4]
        self.assertTrue(screen.embedded_buttons)
        self.assertEqual(screen.sections[0].title, 'Доступ к сервису')
        request = screen.sections[0].rows[0][0]
        self.assertEqual(request.text, 'Запросить доступ')
        self.assertEqual(request.style, 'primary')
        self.assertEqual([b.type for b in screen.rich(rows).blocks][-2:], ['divider', 'buttons'])
        self.assertEqual(draw.call_args.args[-1], 77)

    async def test_language_selection_timeout_shows_error_and_keeps_retry_possible(self):
        import asyncio
        async def slow_save(*args):
            await asyncio.Event().wait()
        backend = SimpleNamespace(set_locale=AsyncMock(side_effect=slow_save))
        self.query.data = user.button(123, 'English', 'first_locale', 'en').callback_data
        with patch.object(user, 'ONBOARDING_TIMEOUT', 0.01), \
             patch.object(user, 'render', new_callable=AsyncMock) as draw:
            await user.user_action_cb(self.query, self.bot, backend, self.state)
        self.assertIn(user.tr('en', 'action.error.service'), draw.call_args.args[2].lines)
        self.assertIn(self.query.data[2:], user.actions)

    async def test_pending_home_loads_policy_and_requests_concurrently(self):
        import asyncio
        policy_started, requests_started = asyncio.Event(), asyncio.Event()
        async def policy(*args):
            policy_started.set()
            await requests_started.wait()
            return {'enabled': True}
        async def requests(*args, **kwargs):
            requests_started.set()
            await policy_started.wait()
            return {'items': [{'status': 'pending'}]}
        backend = SimpleNamespace(me=AsyncMock(return_value={'role': 'member', 'status': 'pending'}),
            bot_title=AsyncMock(return_value={'title': 'Node Plane'}),
            access_request_policy=policy, request=requests)
        with patch.object(user, 'ONBOARDING_TIMEOUT', 0.1), \
             patch.object(user, 'render', new_callable=AsyncMock) as draw:
            await user.show_home(123, 123, self.bot, backend, self.state, 77)
        self.assertIn(user.tr('en', 'home.waiting'), draw.call_args.args[2].sections[0].lines)
        self.assertEqual(draw.call_args.args[2].sections[0].rows, ())

    async def test_opening_empty_requests_has_an_explicit_screen(self):
        backend = SimpleNamespace(pending_access_requests=AsyncMock(return_value={'items': [], 'next_cursor': None}))
        with patch.object(admin_requests, 'render', new_callable=AsyncMock) as draw:
            await admin_requests.requests_cb(self.query, self.bot, backend, self.state)
        self.assertIn(user.tr('en', 'requests.empty'), draw.call_args.args[2].lines)

    async def test_request_search_depends_on_total_pending_not_current_page(self):
        for total, expected in ((0, False), (5, False), (6, True), (12, True)):
            self.state_data.update(request_cursors=[None, 'last'], request_search=None)
            item = {'id': 'request', 'account_id': 'member', 'telegram_user_id': 456, 'created_at': None}
            backend = SimpleNamespace(pending_access_requests=AsyncMock(return_value={
                'items': [item] if total else [], 'next_cursor': None, 'pending_total': total}))
            with patch.object(admin_requests, 'render', new_callable=AsyncMock) as draw:
                await admin_requests.render_request_page(123, 123, 77, self.bot,
                    backend, self.state, 1 if total == 12 else 0)
            screen, rows = draw.call_args.args[2:4]
            callbacks = [b.callback_data for row in screen.fallback_rows(rows) for b in row]
            self.assertEqual('request_search' in callbacks, expected)
        self.state_data.update(request_search='alice', request_cursors=[None])
        backend.pending_access_requests.return_value = {'items': [], 'next_cursor': None, 'pending_total': 5}
        with patch.object(admin_requests, 'render', new_callable=AsyncMock) as draw:
            await admin_requests.render_request_page(123, 123, 77, self.bot, backend, self.state, 0)
        screen, rows = draw.call_args.args[2:4]
        callbacks = [b.callback_data for row in screen.fallback_rows(rows) for b in row]
        self.assertNotIn('request_search', callbacks)
        self.assertIn('request_search_clear', callbacks)

    async def test_admin_menu_has_profiles_without_separate_accounts(self):
        with patch.object(user, 'render', new_callable=AsyncMock) as draw:
            await user.show_admin_menu(1, 101, 5, self.bot,
                SimpleNamespace(bot_title=AsyncMock(return_value={'title': 'Node Plane'}),
                    admin_overview=AsyncMock(side_effect=BackendError('backend_unavailable', 503))), self.state)
        screen, rows = draw.call_args.args[2:4]
        callbacks = [b.callback_data for row in screen.fallback_rows(rows) for b in row]
        self.assertFalse(any(value.startswith('accounts') for value in callbacks))
        self.assertTrue(any(value.startswith('admin_profiles') for value in callbacks))

    async def test_admin_home_table_attention_and_embedded_navigation_are_localized(self):
        overview = {'nodes_enabled': 2, 'nodes_total': 3, 'profiles_active': 4,
            'profiles_total': 5, 'pending_requests': 2,
            'problem_nodes': [{'key': 'lv1', 'title': 'Latvia', 'flag': '🇱🇻'}]}
        backend = SimpleNamespace(bot_title=AsyncMock(return_value={'title': 'Node Plane'}),
                                  admin_overview=AsyncMock(return_value=overview))
        for locale in ('ru', 'en'):
            self.state_data['locale'] = locale
            with patch.object(user, 'render', new_callable=AsyncMock) as draw:
                await user.show_admin_menu(123, 123, 77, self.bot, backend, self.state)
            screen, rows = draw.call_args.args[2:4]
            self.assertEqual(screen.title, user.tr(locale, 'admin.menu') + ' · Node Plane')
            access = next(section for section in screen.sections
                if section.title.startswith(user.tr(locale, 'admin.rich.access_management')))
            self.assertEqual(access.title, user.tr(locale, 'admin.rich.access_management') +
                ' · ' + user.tr(locale, 'requests.pending_count', count=2))
            self.assertEqual(screen.sections[0].lines, ())
            blocks = screen.rich(rows).blocks
            table = next(block for block in blocks if block.type == 'table')
            self.assertEqual(table.cells[1][1].text, '2/3')
            self.assertEqual(table.cells[3][1].text, '2')
            self.assertEqual(table.cells[0][0].text, user.tr(locale, 'admin.rich.item'))
            attention = next(section for section in screen.sections
                if section.title == user.tr(locale, 'admin.rich.attention'))
            self.assertEqual([button.style for button in attention.rows[0]], ['primary', 'primary'])
            self.assertTrue(screen.embedded_buttons)
            self.assertEqual([block.type for block in blocks[-2:]], ['divider', 'buttons'])
            self.assertEqual(sum(block.type == 'divider' for block in blocks), 1)
            callbacks = [b.callback_data for row in screen.fallback_rows(rows) for b in row]
            for callback in ('admin_status', 'announce_menu', 'admin_problem_nodes'):
                self.assertIn(callback, callbacks)
        overview.update(pending_requests=0, problem_nodes=[])
        with patch.object(user, 'render', new_callable=AsyncMock) as draw:
            await user.show_admin_menu(123, 123, 77, self.bot, backend, self.state)
        self.assertFalse(any(section.title == user.tr('en', 'admin.rich.attention')
                             for section in draw.call_args.args[2].sections))
        screen, rows = draw.call_args.args[2:4]
        self.assertFalse(any(section.title.startswith(user.tr('en', 'admin.rich.access_management')) for section in screen.sections))
        self.assertFalse(any(b.callback_data == admin_requests.RequestsCallback().pack() for row in screen.fallback_rows(rows) for b in row))

    async def test_slow_admin_overview_does_not_block_navigation(self):
        import asyncio
        async def slow_overview(*args):
            await asyncio.Event().wait()
        backend = SimpleNamespace(bot_title=AsyncMock(return_value={'title': 'Node Plane'}),
                                  admin_overview=slow_overview)
        with patch.object(user, 'ADMIN_MENU_TIMEOUT', 0.01), \
             patch.object(user, 'render', new_callable=AsyncMock) as draw:
            await user.show_admin_menu(123, 123, 77, self.bot, backend, self.state)
        screen, rows = draw.call_args.args[2:4]
        self.assertIn(user.tr('en', 'admin.rich.overview_unavailable'), screen.plain())
        self.assertTrue(any(b.callback_data.startswith('admin_nodes')
            for row in screen.fallback_rows(rows) for b in row))

    async def test_admin_summary_permission_failure_is_not_treated_as_optional(self):
        backend = SimpleNamespace(bot_title=AsyncMock(return_value={'title': 'Node Plane'}),
            admin_overview=AsyncMock(side_effect=BackendError('permission_denied', 403)))
        with patch.object(user, 'render', new_callable=AsyncMock) as draw:
            with self.assertRaises(BackendError):
                await user.show_admin_menu(123, 123, 77, self.bot, backend, self.state)
        draw.assert_not_awaited()

    async def test_request_review_collapses_ids_and_keeps_decisions_beside_context(self):
        item = {'id': 'request-1', 'account_id': 'account-1', 'telegram_user_id': 456,
            'first_name': 'Alex', 'username': 'alex', 'created_at': '2026-10-04T12:00:00Z'}
        self.state_data.update(request_page_index=2, request_search='alex')
        backend = SimpleNamespace(pending_access_request=AsyncMock(return_value=item))
        for locale in ('ru', 'en'):
            self.state_data['locale'] = locale
            with patch.object(admin_requests, 'render', new_callable=AsyncMock) as draw:
                await admin_requests.review_cb(self.query,
                    admin_requests.ReviewCallback(request_id='request-1'), self.bot, backend, self.state)
            screen, rows = draw.call_args.args[2:4]
            self.assertNotIn('456', ' '.join(screen.lines))
            details = next(block for block in screen.rich(rows).blocks if block.type == 'details')
            self.assertFalse(details.is_open)
            self.assertIn('request-1', ' '.join(block.text for block in details.blocks))
            self.assertEqual([b.style for b in rows[0]], ['primary', 'danger'])
            self.assertEqual(rows[-1][0].callback_data, 'request_page:2')
            self.assertEqual([b.type for b in screen.rich(rows).blocks[-2:]], ['divider', 'buttons'])

    async def test_request_search_back_preserves_filter_and_page_and_exits_input(self):
        self.state_data.update(request_page_index=1, request_cursors=[None, 'cursor'], request_search='alex')
        with patch.object(admin_requests, 'render', new_callable=AsyncMock) as draw:
            await admin_requests.request_search_cb(self.query, self.bot, self.state)
        self.query.data = draw.call_args.args[3][-1][0].callback_data
        self.assertEqual(self.query.data, 'request_page:1')
        backend = SimpleNamespace(pending_access_requests=AsyncMock(return_value={
            'items': [{'id': 'r', 'account_id': 'account', 'first_name': 'Alex'}], 'next_cursor': None}))
        with patch.object(admin_requests, 'render', new_callable=AsyncMock) as draw:
            await admin_requests.request_page_cb(self.query, self.bot, backend, self.state)
        backend.pending_access_requests.assert_awaited_once_with(123, cursor='cursor', search='alex', limit=10)
        self.assertIsNone(self.current_state)
        screen, rows = draw.call_args.args[2:4]
        self.assertEqual(rows[0][0].text, '←')
        self.assertEqual(rows[0][0].callback_data, 'request_page:0')
        self.assertIn('Search: alex', screen.plain())

    async def test_add_user_uses_automatic_profile_and_revision_header(self):
        message = SimpleNamespace(from_user=SimpleNamespace(id=101), chat=SimpleNamespace(id=1, type='private'),
                                  text='102', delete=AsyncMock())
        self.bot.get_chat = AsyncMock(return_value=SimpleNamespace(username='alice', first_name='Alice', last_name=None))
        backend = SimpleNamespace(resolve=AsyncMock(return_value={'id': 'account', 'status': 'pending'}),
            request=AsyncMock(return_value={'revision': 2}),
            profiles=AsyncMock(return_value={'items': [{'id': 'profile'}]}))
        with patch.object(admin_profiles, 'start_profile_setup', new_callable=AsyncMock) as show:
            await admin_profiles.add_profile_user_text(message, self.bot, backend, self.state)
        backend.resolve.assert_awaited_once_with(102, username='alice', first_name='Alice', last_name=None)
        change = backend.request.call_args
        self.assertEqual(change.kwargs['revision'], 2)
        self.assertEqual(change.kwargs['body'], {'status': 'approved'})
        self.assertEqual(show.call_args.args[3], 'profile')

    async def test_awg_preset_selection_updates_desired_settings_only(self):
        backend = SimpleNamespace(request=AsyncMock(return_value={
            'desired_revision': 4, 'settings': {'awg_port': 51820, 'awg_i1_preset': 'quic'}}),
            edit_node=AsyncMock())
        from telegram_client.routers import admin_node_tools
        for preset in ('quic', 'dns', 'chaos'):
            self.query.data = f'node_awg_preset:{preset}:lv1'
            with patch.object(admin_node_tools, 'show_section', new_callable=AsyncMock):
                await admin_nodes.select_awg_preset(self.query, self.bot, backend, self.state)
            body = backend.edit_node.call_args.args[3]
            self.assertEqual(body['settings'], {'awg_port': 51820, 'awg_i1_preset': preset})
        self.assertEqual(backend.request.call_count, 3)

    async def test_node_action_preserves_command_idempotency_header(self):
        client = BackendClient.__new__(BackendClient)
        client.request = AsyncMock(return_value={'id': 'job'})
        await client.node_action(101, 'lv1', 'reinstall_clean', 4, 'stable-key')
        self.assertTrue(client.request.call_args.kwargs['command'])
        self.assertEqual(client.request.call_args.kwargs['command_key'], 'stable-key')

    async def test_member_node_transport_reads_all_backend_pages(self):
        client = BackendClient.__new__(BackendClient)
        client.request = AsyncMock(side_effect=[
            {'items': [{'key': f'n{i}'} for i in range(100)], 'next_cursor': 'cursor+/='},
            {'items': [{'key': 'n100'}], 'next_cursor': None}])
        result = await client.profile_nodes(123, 'p1')
        self.assertEqual(len(result['items']), 101)
        self.assertIsNone(result['next_cursor'])
        self.assertIn('cursor=cursor%2B%2F%3D', client.request.call_args.args[1])
        self.assertEqual(client.request.call_args.kwargs['telegram_user_id'], 123)

    async def test_member_nodes_reject_repeating_backend_cursor(self):
        client = BackendClient.__new__(BackendClient)
        client.request = AsyncMock(return_value={'items': [], 'next_cursor': 'same'})
        with self.assertRaises(BackendError):
            await client.profile_nodes(123, 'p1')

    async def test_backup_confirmation_preserves_key_and_action_label(self):
        from telegram_client.routers import admin_backups
        backend = SimpleNamespace(backup_command=AsyncMock(return_value={'id': 'job'}),
            backup_job=AsyncMock(return_value={'status': 'awaiting_executor', 'action': 'create', 'result': None}))
        self.state_data['locale'] = 'en'
        self.query.data = 'backup_create'
        with patch.object(admin_backups, 'render', new_callable=AsyncMock) as draw:
            await admin_backups.backup_cb(self.query, self.bot, backend, self.state)
            self.assertEqual(draw.call_args.args[3][0][1].text, user.tr('en', 'backups.create'))
            draft = self.state_data['backup_draft']
            self.query.data = 'backup_submit:' + draft['nonce']
            await admin_backups.backup_cb(self.query, self.bot, backend, self.state)
            await admin_backups.backup_cb(self.query, self.bot, backend, self.state)
        self.assertEqual(backend.backup_command.call_args_list[0].args[2], draft['key'])
        self.assertEqual(backend.backup_command.call_args_list[1].args[2], draft['key'])

    async def test_backup_incompatible_snapshot_has_no_restore_button_and_keeps_page(self):
        from telegram_client.routers import admin_backups
        backend = SimpleNamespace(backup_detail=AsyncMock(return_value={
            'id': 'snapshot', 'created_at': '2026-09-30', 'app_version': '0.4.3',
            'profiles': 2, 'nodes': 1, 'compatible': False}))
        self.state_data['backup_offset'] = 8
        self.query.data = 'backup_detail:snapshot'
        with patch.object(admin_backups, 'render', new_callable=AsyncMock) as draw:
            await admin_backups.backup_cb(self.query, self.bot, backend, self.state)
        callbacks = [b.callback_data for row in draw.call_args.args[3] for b in row]
        self.assertEqual(callbacks, ['backup_list:8'])

    def setUp(self):
        self.bot = SimpleNamespace(send_document=AsyncMock(), send_photo=AsyncMock())
        self.state_data = {}
        self.current_state = None
        async def get_data():
            return dict(self.state_data)
        async def update_data(**values):
            self.state_data.update(values)
        async def clear():
            self.state_data.clear()
        async def set_state(value):
            self.current_state = value.state if hasattr(value, 'state') else value
        async def get_state():
            return self.current_state
        self.state = SimpleNamespace(get_data=get_data, update_data=update_data,
            clear=clear, set_state=set_state, get_state=get_state)
        self.query = SimpleNamespace(answer=AsyncMock(),
            from_user=SimpleNamespace(id=123),
            message=SimpleNamespace(chat=SimpleNamespace(id=123, type='private'),
                                    message_id=77))

    async def test_update_actions_hidden_when_live_commits_are_current(self):
        from telegram_client.routers import admin_updates
        backend = SimpleNamespace(update_rollout=AsyncMock(return_value={
            'desired_version': '0.4.3', 'desired_commit': 'a' * 40,
            'driver_status': 'current', 'driver': {'commit': 'a' * 40},
            'nodes': [{'title': 'Latvia', 'agent_status': 'current', 'runtime_status': 'current'}],
            'agents_required': False, 'runtimes_required': False, 'latest_job': None}))
        with patch.object(admin_updates, 'render', new_callable=AsyncMock) as draw:
            await admin_updates.show_fleet(self.query, self.bot, backend, self.state)
        callbacks = [b.callback_data for row in draw.call_args.args[3] for b in row]
        self.assertNotIn('ufleet_confirm:agents', callbacks)
        self.assertNotIn('ufleet_confirm:runtimes', callbacks)
        backend.update_rollout.return_value['agents_required'] = True
        with patch.object(admin_updates, 'render', new_callable=AsyncMock) as draw:
            await admin_updates.show_fleet(self.query, self.bot, backend, self.state)
        self.assertIn('ufleet_confirm:agents', [b.callback_data for row in draw.call_args.args[3] for b in row])

    async def test_announcement_double_tap_reuses_command_key(self):
        from telegram_client.routers import admin_announcements
        self.state_data['announcement_draft']={'text':'Hello','nonce':'test','key':'stable-key'}
        self.query.data='announce_send:test'
        backend=SimpleNamespace(announcement_create=AsyncMock(return_value={'id':'job'}),
            announcement_status=AsyncMock(return_value={'total':1,'status':'running',
                'counts':dict(queued=1,claimed=0,sent=0,failed=0,unknown=0,skipped=0)}))
        with patch.object(admin_announcements,'render',new_callable=AsyncMock):
            await admin_announcements.announcement_cb(self.query,self.bot,backend,self.state)
            await admin_announcements.announcement_cb(self.query,self.bot,backend,self.state)
        self.assertEqual([call.args[2] for call in backend.announcement_create.call_args_list],['stable-key','stable-key'])

    async def test_announcement_edit_back_and_preview_share_row(self):
        from telegram_client.routers import admin_announcements
        self.state_data['announcement_draft']={'text':'Hello','nonce':'test','key':'stable-key'}
        self.query.data='announce_edit'
        backend=SimpleNamespace(me=AsyncMock(return_value={'permissions':['settings.manage']}))
        with patch.object(admin_announcements,'render',new_callable=AsyncMock) as draw:
            await admin_announcements.announcement_cb(self.query,self.bot,backend,self.state)
        self.assertEqual([b.callback_data for b in draw.call_args.args[3][0]],['announce_menu','announce_preview'])

    async def test_announcement_transport_sends_silently_and_acknowledges(self):
        from telegram_client.announcement_delivery import deliver_one
        bot=SimpleNamespace(send_rich_message=AsyncMock())
        backend=SimpleNamespace(announcement_claim=AsyncMock(return_value={'delivery':
            {'id':'delivery','telegram_user_id':102,'text':'Hello','locale':'ru','silent':True}}),
            announcement_ack=AsyncMock())
        self.assertTrue(await deliver_one(bot,backend))
        self.assertTrue(bot.send_rich_message.call_args.kwargs['disable_notification'])
        self.assertEqual(backend.announcement_ack.call_args.args[2],'sent')
        self.assertEqual(backend.announcement_claim.call_args.args[0],backend.announcement_ack.call_args.args[1])

    async def test_announcement_transport_does_not_replay_ambiguous_send(self):
        from telegram_client.announcement_delivery import deliver_one
        bot=SimpleNamespace(send_rich_message=AsyncMock(side_effect=TimeoutError()))
        backend=SimpleNamespace(announcement_claim=AsyncMock(return_value={'delivery':
            {'id':'delivery','telegram_user_id':102,'text':'Hello','silent':False}}),
            announcement_ack=AsyncMock())
        await deliver_one(bot,backend)
        self.assertEqual(bot.send_rich_message.await_count,1)
        self.assertEqual(backend.announcement_ack.call_args.args[2],'unknown')

    async def test_alert_policy_selected_interval_and_back_navigation(self):
        from telegram_client.routers import admin_alerts
        backend=SimpleNamespace(alerts_overview=AsyncMock(return_value={
            'enabled':True,'notify_resolved':True,'interval_minutes':15,'active_count':0,'active':[],
            'last_scan':None,'delivery_counts':dict(queued=0,claimed=0,sent=0,failed=0,unknown=0,skipped=0)}))
        self.query.data='alerts'
        self.state_data['locale']='en'
        with patch.object(admin_alerts,'render',new_callable=AsyncMock) as draw:
            await admin_alerts.alerts_cb(self.query,self.bot,backend,self.state)
        rows=draw.call_args.args[3]
        self.assertEqual(rows[1][1].text,'15 min')
        self.assertEqual(rows[1][1].style, 'primary')
        self.assertIsNone(rows[1][0].style)
        self.assertTrue(rows[-1][0].callback_data.startswith('admin_settings'))

    async def test_active_alert_pagination_and_localized_transport(self):
        from telegram_client.routers import admin_alerts
        from telegram_client.alert_delivery import alert_screen
        backend=SimpleNamespace(alerts_overview=AsyncMock(return_value={'active':[
            {'title':str(i),'kind':'xray_down'} for i in range(9)]}))
        self.query.data='alerts_active:8'
        with patch.object(admin_alerts,'render',new_callable=AsyncMock) as draw:
            await admin_alerts.alerts_cb(self.query,self.bot,backend,self.state)
        rows=draw.call_args.args[3]
        self.assertEqual([button.callback_data for button in rows[0]],['alerts_active:0'])
        screen=alert_screen({'locale':'ru','event':{'kind':'node_unreachable','resolved':True,
            'at':'2026-09-30T10:00:00+00:00','payload':{'node_title':'Moscow'}}})
        self.assertIn('устранена',screen.title)
        self.assertIn('Moscow',screen.plain())

    async def test_update_confirmation_uses_same_key_on_double_tap(self):
        from telegram_client.routers import admin_updates
        backend = SimpleNamespace(run_update=AsyncMock(return_value={'id': 'job'}),
            update_job=AsyncMock(return_value={'id': 'job', 'status': 'awaiting_executor', 'items': []}))
        with patch.object(admin_updates, 'render', new_callable=AsyncMock):
            await admin_updates.confirm(self.query, self.bot, self.state,
                {'kind': 'version', 'branch': 'dev', 'target_ref': 'v0.4.3'}, ['Confirm'])
            draft = self.state_data['update_draft']
            self.query.data = 'update_submit:' + draft['nonce']
            await admin_updates.update_tools_cb(self.query, self.bot, backend, self.state)
            await admin_updates.update_tools_cb(self.query, self.bot, backend, self.state)
        self.assertEqual(backend.run_update.call_args_list[0].args[2], draft['key'])
        self.assertEqual(backend.run_update.call_args_list[1].args[2], draft['key'])

    async def test_update_catalog_pagination_and_blocked_selection(self):
        from telegram_client.routers import admin_updates
        backend = SimpleNamespace(update_versions=AsyncMock(return_value={
            'items': [{'version': '0.3.0', 'ref': 'v0.3.0', 'allowed': False,
                       'action': 'blocked', 'reason': 'pre1_minor_downgrade_blocked'}],
            'branch': 'dev', 'offset': 8, 'next_offset': 16, 'total': 20, 'status': 'ok'}),
            run_update=AsyncMock())
        with patch.object(admin_updates, 'render', new_callable=AsyncMock) as draw:
            await admin_updates.show_versions(self.query, self.bot, backend, self.state, 8)
            callbacks = [b.callback_data for row in draw.call_args.args[3] for b in row]
            self.assertIn('uv_page:0', callbacks)
            self.assertIn('uv_page:16', callbacks)
            self.query.data = 'uv_select:' + self.state_data['update_catalog_nonce'] + ':0'
            await admin_updates.update_tools_cb(self.query, self.bot, backend, self.state)
        backend.run_update.assert_not_called()
        self.assertNotIn('update_draft', self.state_data)

    async def test_member_profile_comes_from_own_account(self):
        backend = SimpleNamespace(profiles=AsyncMock(return_value={'items': [
            {'id': 'p1', 'display_name': 'Personal'}]}),
            profile_nodes=AsyncMock(return_value={'items': [
                {'key': 'lv1', 'title': 'Latvia', 'region': 'EU', 'flag': '🇱🇻',
                 'protocols': [{'kind': 'xray', 'transports': ['tcp', 'xhttp']}]}]}),
            member_profile_summary=AsyncMock(return_value={
                'display_name': 'Personal', 'frozen': False, 'expired': False}))
        with patch.object(user, 'render', new_callable=AsyncMock) as render:
            await user.show_profiles(123, 123, 77, self.bot, backend, self.state)
            backend.profiles.assert_awaited_once_with(123)
            backend.profile_nodes.assert_awaited_once_with(123, 'p1')
            await user.show_profile(123, 123, 77, 'p1', self.bot, backend, self.state)
            self.assertEqual(backend.profile_nodes.await_count, 2)
            await user.show_node(123, 123, 77, 'p1', 'lv1', self.bot, backend, self.state)
            self.assertEqual(backend.profile_nodes.await_count, 3)
            self.assertEqual(len(render.call_args.args[3]), 2)

    async def test_member_account_shows_profile_access_and_statistics(self):
        summary = {'profile_id': 'p1', 'display_name': 'Personal',
            'frozen': False, 'expired': False, 'expires_at': None,
            'created_at': '2026-09-29T09:00:00+00:00',
            'nodes': [{'key': 'lv1', 'title': 'Latvia', 'flag': '🇱🇻', 'region': 'Europe',
                       'protocols': ['awg', 'xray']}],
            'node_count': 1, 'protocol_count': 2, 'awg_count': 1,
            'xray_count': 1, 'issued_count': 3,
            'last_issued_at': '2026-09-29T10:00:00+00:00'}
        backend = SimpleNamespace(
            me=AsyncMock(return_value={'id': 'account1', 'status': 'approved'}),
            profiles=AsyncMock(return_value={'items': [
                {'id': 'p1', 'display_name': 'Personal'}]}),
            member_profile_summary=AsyncMock(return_value=summary))
        with patch.object(user, 'render', new_callable=AsyncMock) as render:
            await user.show_account_info(123, 123, 77, self.bot, backend,
                                         self.state, username='alice')
            profile_screen = render.call_args.args[2]
            self.assertIn('Telegram username: @alice', profile_screen.lines)
            self.assertFalse(profile_screen.sections[0].collapsed)
            self.assertIn('Configs issued: 3', profile_screen.sections[0].lines)
            servers = profile_screen.sections[-1]
            self.assertTrue(servers.collapsed)
            self.assertEqual(servers.sections[0].title, 'Europe')
            self.assertEqual(servers.sections[0].sections[0].title, '🇱🇻 Latvia')
            self.assertEqual(servers.sections[0].sections[0].lines, ('AmneziaWG', 'VLESS'))
            self.assertNotEqual(servers.rich()[0].blocks[-1].type, 'divider')
            blocks = profile_screen.rich(render.call_args.args[3]).blocks
            self.assertEqual([block.type for block in blocks][-2:], ['divider', 'buttons'])
            self.assertEqual(servers.rows, ())
            self.assertEqual(len(render.call_args.args[3]), 1)
            await user.show_account_stats(123, 123, 77, 'p1', self.bot,
                                          backend, self.state)
            stats_screen = render.call_args.args[2]
        self.assertIn('Configs issued: 3', stats_screen.lines)
        self.assertIn('Profile created: 2026-09-29', stats_screen.lines)
        summary['expired'] = True
        with patch.object(user, 'render', new_callable=AsyncMock) as render:
            await user.show_account_profile(123, 123, 77, 'p1', self.bot,
                                            backend, self.state)
        self.assertIn('Status: Expired', render.call_args.args[2].lines)
        backend.member_profile_summary.assert_awaited_with(123, 'p1')

    def test_profile_monthly_traffic_totals_and_per_node_protocol_breakdown(self):
        summary = {'nodes': [], 'traffic': {'status': 'current', 'month': '2026-10',
            'items': [{'protocol': 'awg', 'uplink_bytes': 1024, 'downlink_bytes': 2048},
                      {'protocol': 'xray', 'uplink_bytes': 1024, 'downlink_bytes': 0}],
            'nodes': [{'node_key': 'lv1', 'protocol': 'awg', 'uplink_bytes': 1024,
                       'downlink_bytes': 2048, 'status': 'current'}]}}
        self.assertIn('Total for 2026-10: 4.0 KiB', user.profile_statistics(summary, 'en'))
        node = {'key': 'lv1', 'protocols': ['awg', 'xray']}
        lines = user.profile_node_traffic(summary, node, 'en')
        self.assertIn('AmneziaWG · 2026-10: 3.0 KiB', lines[0])
        self.assertIn('VLESS: waiting', lines[1])
        for traffic in (None, {'status': 'consent_required', 'items': []}):
            summary['traffic'] = traffic
            self.assertEqual(user.profile_node_traffic(summary, node, 'en'), ('AmneziaWG', 'VLESS'))
            self.assertFalse(any('KiB' in line for line in user.profile_statistics(summary, 'en')))

    async def test_awg_screen_embeds_collapsed_qr_monospace_uri_and_both_files(self):
        backend = SimpleNamespace(
            issuance=AsyncMock(return_value={'status': 'succeeded',
                'profile_id': 'p1', 'node_key': 'lv1', 'protocol': 'awg', 'transport': 'vpn'}),
            artifact=AsyncMock(return_value={'filename': 'Latvia.vpn', 'content': 'vpn://fresh-config',
                'files': [{'filename': 'Latvia.vpn', 'content': 'vpn://fresh-config'},
                          {'filename': 'Latvia.conf', 'content': '[Interface]\nPrivateKey = test'}]}))
        with patch.object(user, 'render', new_callable=AsyncMock, return_value=True) as draw:
            await user.show_issuance(123, 123, 77, 'issuance1', self.bot, backend, self.state)
        screen = draw.call_args.args[2]
        blocks = screen.rich().blocks
        self.assertFalse(blocks[2].is_open)
        self.assertEqual(blocks[2].blocks[0].type, 'photo')
        self.assertFalse(blocks[3].is_open)
        self.assertEqual(blocks[3].blocks[0].text.type, 'code')
        self.assertEqual(blocks[3].blocks[0].text.text, 'vpn://fresh-config')
        files = next(block for block in blocks if block.type == 'details'
                     and block.summary == 'Configuration files')
        self.assertFalse(files.is_open)
        self.assertEqual([block.document.media.filename for block in files.blocks],
                         ['Latvia.vpn', 'Latvia.conf'])
        self.assertFalse(any(block.type == 'document' for block in blocks))
        self.bot.send_document.assert_not_awaited()
        self.bot.send_photo.assert_not_awaited()
        back = user.actions[draw.call_args.args[3][-1][0].callback_data[2:]]
        self.assertEqual((back.name, back.args), ('profile', ('p1',)))

    async def test_vless_screen_uses_same_qr_and_uri_layout(self):
        backend = SimpleNamespace(
            issuance=AsyncMock(return_value={'status': 'succeeded',
                'profile_id': 'p1', 'node_key': 'lv1', 'protocol': 'xray', 'transport': 'tcp'}),
            artifact=AsyncMock(return_value={'filename': 'Latvia.txt', 'content': 'vless://test'}))
        with patch.object(user, 'render', new_callable=AsyncMock, return_value=True) as draw:
            await user.show_issuance(123, 123, 77, 'issuance1', self.bot, backend, self.state)
        blocks = draw.call_args.args[2].rich().blocks
        self.assertFalse(blocks[2].is_open)
        self.assertFalse(blocks[3].is_open)
        self.assertEqual(blocks[3].blocks[0].text.type, 'code')
        files = next(block for block in blocks if block.type == 'details'
                     and block.summary == 'Configuration files')
        self.assertFalse(files.is_open)
        self.assertEqual(files.blocks[0].document.media.filename, 'Latvia.txt')
        self.assertEqual(blocks[3].blocks[0].text.text, 'vless://test')
        back = user.actions[draw.call_args.args[3][-1][0].callback_data[2:]]
        self.assertEqual((back.name, back.args), ('protocol', ('p1', 'lv1', 'xray')))

    async def test_awg_selection_issues_bundle_without_format_selector(self):
        backend = SimpleNamespace(profile_nodes=AsyncMock(return_value={'items': [
            {'key': 'lv1', 'title': 'Latvia', 'protocols': [{'kind': 'awg'}]}]}))
        with patch.object(user, 'issue', new_callable=AsyncMock) as issue:
            await user.show_protocol(123, 123, 77, 'p1', 'lv1', 'awg', self.bot, backend, self.state)
        self.assertEqual(issue.call_args.args[3:7], ('p1', 'lv1', 'awg', 'vpn'))

    async def test_server_sections_bind_protocol_buttons_to_the_correct_node(self):
        backend = SimpleNamespace(
            profiles=AsyncMock(return_value={'items': [{'id': 'p1'}]}),
            member_profile_summary=AsyncMock(return_value={'display_name': 'Alice',
                'frozen': False, 'expired': False}),
            profile_nodes=AsyncMock(return_value={'items': [
                {'key': 'lv1', 'title': 'Latvia', 'flag': '🇱🇻', 'region': 'EU',
                 'protocols': [{'kind': 'awg'}, {'kind': 'xray'}]},
                {'key': 'msk1', 'title': 'Moscow', 'flag': '🇷🇺', 'region': 'RU',
                 'protocols': [{'kind': 'xray'}]}]}))
        with patch.object(user, 'render', new_callable=AsyncMock) as draw:
            await user.show_profiles(123, 123, 77, self.bot, backend, self.state)
        screen = draw.call_args.args[2]
        self.assertTrue(screen.embedded_buttons)
        self.assertEqual([section.title for section in screen.sections], ['EU', 'RU'])
        self.assertEqual([section.sections[0].title for section in screen.sections], ['🇱🇻 Latvia', '🇷🇺 Moscow'])
        self.assertEqual(screen.lines, ())
        self.assertEqual([block.type for block in screen.rich(draw.call_args.args[3]).blocks],
            ['heading', 'heading', 'heading', 'buttons', 'divider', 'heading', 'heading', 'buttons', 'divider', 'buttons'])
        backend.member_profile_summary.assert_not_awaited()
        for section, node_key in zip(screen.sections, ('lv1', 'msk1')):
            for button in section.sections[0].rows[0]:
                action = user.actions[button.callback_data[2:]]
                self.assertEqual(action.name, 'protocol')
                self.assertEqual(action.args[:2], ('p1', node_key))
        back = user.actions[draw.call_args.args[3][-1][0].callback_data[2:]]
        self.assertEqual(back.name, 'home')

    async def test_config_server_pages_group_regions_and_preserve_return_page(self):
        nodes = [{'key': f'n{i:02}', 'title': f'Server {i:02}',
                  'region': 'Europe' if i < 12 else 'Asia',
                  'protocols': [{'kind': 'xray'}]} for i in range(23)]
        backend = SimpleNamespace(profile_nodes=AsyncMock(return_value={'items': nodes[::-1]}))
        seen = []
        with patch.object(user, 'render', new_callable=AsyncMock) as draw:
            for page in range(3):
                await user.show_profile(123, 123, 77, 'p1', self.bot, backend, self.state,
                                        page_index=page)
                screen, rows = draw.call_args.args[2:4]
                titles = [node.title for region in screen.sections for node in region.sections]
                self.assertEqual(len(titles), 10 if page < 2 else 3)
                seen.extend(titles)
                self.assertIn(f'{page + 1} / 3', [b.text for b in rows[0]])
                self.assertEqual([b.text for b in rows[0]],
                    (['←'] if page else []) + [f'{page + 1} / 3'] + (['→'] if page < 2 else []))
                for region in screen.sections:
                    self.assertTrue(all(region.title not in node.title for node in region.sections))
            await user.show_profile(123, 123, 77, 'p1', self.bot, backend, self.state)
        self.assertEqual(len(set(seen)), 23)
        self.assertEqual(seen, [f'Server {i:02}' for i in [*range(12, 23), *range(12)]])
        self.assertIn('3 / 3', [b.text for b in draw.call_args.args[3][0]])

    async def test_profile_pagination_keeps_statistics_and_expands_servers(self):
        self.query.from_user.username = 'alice'
        nodes = [{'key': f'n{i:02}', 'title': f'Server {i:02}', 'flag': '',
                  'region': 'Europe', 'protocols': ['awg']} for i in range(11)]
        summary = {'display_name': 'Alice', 'frozen': False, 'expired': False,
                   'nodes': nodes, 'issued_count': 15, 'node_count': 11}
        backend = SimpleNamespace(member_profile_summary=AsyncMock(return_value=summary))
        with patch.object(user, 'render', new_callable=AsyncMock) as draw:
            await user.show_account_profile(123, 123, 77, 'p1', self.bot, backend, self.state,
                                            username='alice')
            first = draw.call_args.args[2]
            servers = first.sections[-1]
            self.assertEqual(len(servers.sections[0].sections), 10)
            self.assertFalse(servers.is_open)
            self.query.data = servers.rows[0][-1].callback_data
            await user.user_action_cb(self.query, self.bot, backend, self.state)
            second = draw.call_args.args[2]
            self.assertEqual(first.lines, second.lines)
            self.assertEqual(first.sections[0], second.sections[0])
            self.assertEqual(second.sections[-1].sections[0].sections[0].title, 'Server 10')
            self.assertEqual(len(second.sections[-1].sections[0].sections), 1)
            self.assertTrue(second.sections[-1].rich()[0].is_open)
            self.assertEqual(draw.call_args.args[-1], 77)
            backend.member_profile_summary.assert_awaited_with(123, 'p1')

    async def test_server_pagination_hidden_at_ten_and_clamps_after_deletion(self):
        for count in (0, 1, 10):
            nodes, index, pages = user.server_page([
                {'key': str(i), 'title': str(i), 'region': 'Europe'} for i in range(count)], 99)
            self.assertEqual((index, pages), (0, 1))
            self.assertEqual(user.server_pagination(123, 'en', index, pages, 'profile'), ())
            self.assertEqual(len(nodes), count)

    async def test_transport_selection_goes_back_to_server_list(self):
        backend = SimpleNamespace(profile_nodes=AsyncMock(return_value={'items': [
            {'key': 'lv1', 'title': 'Latvia', 'protocols': [
                {'kind': 'xray', 'transports': ['tcp', 'xhttp']}]}]}))
        with patch.object(user, 'render', new_callable=AsyncMock) as draw:
            await user.show_protocol(123, 123, 77, 'p1', 'lv1', 'xray', self.bot, backend, self.state)
        screen = draw.call_args.args[2]
        self.assertEqual(screen.lines, ('Choose a transport.',))
        transport_row = draw.call_args.args[3][0]
        self.assertEqual([button.text for button in transport_row], ['XHTTP', 'TCP'])
        for button, transport in zip(transport_row, ('xhttp', 'tcp')):
            action = user.actions[button.callback_data[2:]]
            self.assertEqual(action.args, ('p1', 'lv1', 'xray', transport))
        back = user.actions[draw.call_args.args[3][-1][0].callback_data[2:]]
        self.assertEqual((back.name, back.args), ('profile', ('p1',)))

    async def test_member_settings_actions_describe_the_change_and_preserve_backend_values(self):
        backend = SimpleNamespace(me=AsyncMock(return_value={
            'announcement_silent': True, 'traffic_available': True, 'traffic_consent': True}))
        with patch.object(user, 'render', new_callable=AsyncMock) as draw:
            await user.show_member_settings(123, 123, 77, self.bot, backend, self.state)
        sections = draw.call_args.args[2].sections
        sound = sections[1].rows[0][0]
        consent = sections[2].rows[0][0]
        self.assertEqual(sound.text, 'Enable')
        self.assertEqual(user.actions[sound.callback_data[2:]].args, ('false',))
        self.assertEqual(consent.text, 'Withdraw consent')
        self.assertEqual(user.actions[consent.callback_data[2:]].args, ('false',))

    async def test_language_and_sound_choices_highlight_only_the_saved_value(self):
        for locale in ('ru', 'en'):
            for silent in (False, True):
                self.state_data['locale'] = locale
                backend = SimpleNamespace(me=AsyncMock(return_value={
                    'locale': locale, 'announcement_silent': silent}))
                with patch.object(user, 'render', new_callable=AsyncMock) as draw:
                    await user.show_member_settings(123, 123, 77, self.bot, backend, self.state)
                self.assertEqual(draw.call_args.args[2].lines, ())
                language, sound = draw.call_args.args[2].sections
                self.assertEqual((language.lines, sound.lines), ((), ()))
                self.assertEqual(len(language.rows[0]), 2)
                self.assertEqual(len(sound.rows[0]), 2)
                self.assertEqual([b.text for b in sound.rows[0]],
                    ['Включить', 'Выключить'] if locale == 'ru' else ['Enable', 'Disable'])
                self.assertEqual([b.style for b in language.rows[0]],
                    ['primary', None] if locale == 'ru' else [None, 'primary'])
                self.assertEqual([b.style for b in sound.rows[0]],
                    [None, 'primary'] if silent else ['primary', None])
                self.assertEqual([user.actions[b.callback_data[2:]].args for b in sound.rows[0]],
                                 [('false',), ('true',)])
                self.assertEqual([b.style for b in sound.rich()[-1].buttons],
                                 [b.style for b in sound.rows[0]])

    async def test_admin_grant_server_lists_include_flags(self):
        backend = SimpleNamespace(admin_nodes=AsyncMock(return_value={'items': [
            {'key': 'lv1', 'title': 'Latvia', 'flag': '🇱🇻', 'protocols': ['awg', 'xray']}]}),
            profile_grants=AsyncMock(return_value={'items': []}),
            request=AsyncMock(return_value={'desired_revision': 1}))
        self.state_data['draft_profile_name'] = 'Alice'
        with patch.object(admin_profiles, 'render', new_callable=AsyncMock) as draw:
            await admin_profiles.show_create_nodes(123, 123, 77, self.bot, backend, self.state)
            self.assertEqual(draw.call_args.args[2].sections[1].sections[0].title, '🇱🇻 Latvia')
            await admin_profiles.show_grant_nodes(123, 123, 77, 'p1', self.bot, backend, self.state)
            self.assertEqual(draw.call_args.args[2].sections[1].sections[0].title, '🇱🇻 Latvia')

    async def test_plain_config_fallback_preserves_downloads(self):
        backend = SimpleNamespace(
            issuance=AsyncMock(return_value={'status': 'succeeded', 'profile_id': 'p1',
                'node_key': 'lv1', 'protocol': 'awg', 'transport': 'vpn'}),
            artifact=AsyncMock(return_value={'filename': 'Latvia.vpn', 'content': 'vpn://test'}))
        with patch.object(user, 'render', new_callable=AsyncMock, return_value=False):
            await user.show_issuance(123, 123, 77, 'issuance1', self.bot, backend, self.state)
        self.assertEqual(self.bot.send_document.call_args.args[1].filename, 'Latvia.vpn')

    async def test_issuance_poll_does_not_replace_screen_after_navigation(self):
        async def completed_after_back(*args):
            await self.state.update_data(issuance_poll_token=None)
            return {'status': 'succeeded'}
        backend = SimpleNamespace(
            issue=AsyncMock(return_value={'id': 'issuance1'}),
            issuance=AsyncMock(side_effect=completed_after_back))
        with patch.object(user, 'render', new_callable=AsyncMock) as render, \
             patch.object(user, 'show_issuance', new_callable=AsyncMock) as show_result:
            await user.issue(123, 123, 77, 'p1', 'lv1', 'awg', 'vpn',
                             self.bot, backend, self.state)
        self.assertEqual(render.await_count, 1)
        show_result.assert_not_awaited()
        self.bot.send_document.assert_not_awaited()

    async def test_artifact_read_does_not_deliver_after_navigation(self):
        async def artifact_after_back(*args):
            await self.state.update_data(issuance_poll_token=None)
            return {'filename': 'config.vpn', 'content': 'vpn://fresh-config'}
        backend = SimpleNamespace(
            issuance=AsyncMock(return_value={'status': 'succeeded',
                'profile_id': 'p1', 'node_key': 'lv1', 'protocol': 'awg',
                'transport': 'vpn'}),
            artifact=AsyncMock(side_effect=artifact_after_back))
        with patch.object(user, 'render', new_callable=AsyncMock) as render:
            await user.show_issuance(123, 123, 77, 'issuance1',
                                     self.bot, backend, self.state)
        render.assert_not_awaited()
        self.bot.send_document.assert_not_awaited()

    async def test_qr_artifact_read_after_back_does_not_send_photo(self):
        async def artifact_after_back(*args):
            await self.state.update_data(issuance_poll_token=None)
            return {'content': 'vpn://fresh-config'}
        backend = SimpleNamespace(
            issuance=AsyncMock(return_value={'protocol': 'awg', 'transport': 'vpn'}),
            artifact=AsyncMock(side_effect=artifact_after_back))
        with patch.object(user, 'render', new_callable=AsyncMock) as render:
            await user.show_qr(123, 123, 77, 'issuance1',
                               self.bot, backend, self.state)
        render.assert_not_awaited()
        self.bot.send_photo.assert_not_awaited()

    async def test_qr_photo_sent_after_back_is_deleted(self):
        async def photo_after_back(*args):
            await self.state.update_data(issuance_poll_token=None)
            return SimpleNamespace(message_id=88)
        self.bot.send_photo.side_effect = photo_after_back
        self.bot.delete_message = AsyncMock()
        backend = SimpleNamespace(
            issuance=AsyncMock(return_value={'protocol': 'awg', 'transport': 'vpn'}),
            artifact=AsyncMock(return_value={'content': 'vpn://fresh-config'}))
        with patch.object(user, 'render', new_callable=AsyncMock) as render:
            await user.show_qr(123, 123, 77, 'issuance1',
                               self.bot, backend, self.state)
        render.assert_not_awaited()
        self.bot.delete_message.assert_awaited_once_with(123, 88)

    async def test_awg_qr_uses_import_payload_and_returns_to_issuance(self):
        self.assertEqual(user.qr_payload('awg', 'vpn', 'vpn://fresh-config'),
                         'fresh-config')
        self.assertEqual(user.qr_payload('xray', 'tcp', 'vless://link'),
                         'vless://link')
        backend = SimpleNamespace(
            issuance=AsyncMock(return_value={'protocol': 'awg', 'transport': 'vpn',
                'profile_id': 'p1', 'node_key': 'lv1'}),
            artifact=AsyncMock(return_value={'content': 'vpn://fresh-config'}))
        self.bot.send_photo.return_value = SimpleNamespace(message_id=88)
        with patch.object(user, 'render', new_callable=AsyncMock) as render:
            await user.show_qr(123, 123, 77, 'issuance1', self.bot, backend,
                               self.state)
        back = render.call_args.args[3][0][0]
        action = user.actions[back.callback_data[2:]]
        self.assertEqual((action.name, action.args), ('qr_back', ('issuance1',)))

    async def test_member_action_error_hides_backend_code(self):
        self.query.from_user.username = 'alice'
        self.query.data = user.button(123, 'Open', 'account_profile', 'p1',
                                      'home').callback_data
        backend = SimpleNamespace(member_profile_summary=AsyncMock(
            side_effect=BackendError('grant_revoked', 403)))
        with patch.object(user, 'render', new_callable=AsyncMock) as render:
            await user.user_action_cb(self.query, self.bot, backend, self.state)
        screen = render.call_args.args[2]
        self.assertNotIn('grant_revoked', ' '.join(screen.lines))
        self.assertIn('access has changed', ' '.join(screen.lines))

    async def test_grants_are_saved_together_with_profile_revision(self):
        backend = SimpleNamespace(
            profile_grants=AsyncMock(return_value={'items': [
                {'node_key': 'lv1', 'protocol': 'awg'}]}),
            request=AsyncMock(return_value={'desired_revision': 7}),
            replace_grants=AsyncMock())
        with patch.object(admin_profiles, 'show_grant_protocols', new_callable=AsyncMock):
            await admin_profiles.change_grant(self.query, 'p1', 'lv1', 'xray',
                True, self.bot, backend, self.state)
        backend.replace_grants.assert_not_awaited()
        self.assertEqual(self.state_data['draft_grants'], [
            {'node_key': 'lv1', 'protocol': 'awg'},
            {'node_key': 'lv1', 'protocol': 'xray'}])
        self.query.data = 'admin_profile_grants_save:p1'
        with patch.object(admin_profiles, 'show_admin_profile', new_callable=AsyncMock):
            await admin_profiles.save_grants_cb(self.query, self.bot, backend, self.state)
        backend.replace_grants.assert_awaited_once_with(123, 'p1', 7,
            [{'node_key': 'lv1', 'protocol': 'awg'},
             {'node_key': 'lv1', 'protocol': 'xray'}])

    async def test_protocol_buttons_pack_required_callback_fields(self):
        backend = SimpleNamespace(
            profile_grants=AsyncMock(return_value={'items': [
                {'node_key': 'lv1', 'protocol': 'awg'}]}),
            request=AsyncMock(return_value={'title': 'Latvia',
                'desired_revision': 1, 'protocols': ['awg', 'xray']}))
        with patch.object(admin_profiles, 'render', new_callable=AsyncMock) as render:
            await admin_profiles.show_grant_protocols(123, 123, 77,
                '00000000-0000-0000-0000-000000000001', 'lv1',
                self.bot, backend, self.state)
        rows = render.call_args.args[3]
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(rows[0]), 2)
        self.assertTrue(all(len(button.callback_data.encode()) <= 64 for row in rows for button in row))

    async def test_node_wizard_saves_then_queues_agent_from_persisted_connection(self):
        self.query.data = 'wizard_proto:done'
        self.state_data.update({
            'create_node_command_key': 'create-key',
            'rollout_command_key': 'rollout-key',
            'wizard_data': {'key': 'lv1', 'title': 'Latvia', 'region': 'EU',
                            'flag': '🇱🇻', 'public_host': 'lv1.example.com',
                            'protocols': ['awg', 'xray'], 'transport': 'ssh',
                            'ssh_target': 'root@lv1.example.com'}})
        backend = SimpleNamespace(create_node=AsyncMock(return_value={'key': 'lv1'}),
            rollout_agent=AsyncMock(return_value={'id': 'rollout-id'}),
            request=AsyncMock(return_value={'transport': 'ssh',
                'ssh_target': 'root@lv1.example.com'}))
        with patch.object(admin_nodes, 'render', new_callable=AsyncMock), \
             patch.object(admin_nodes, 'show_rollout_status', new_callable=AsyncMock):
            await admin_nodes.wizard_proto_cb(self.query, self.bot, backend, self.state)
            self.assertIsNone(backend.create_node.await_args)
            self.assertIsNone(backend.rollout_agent.await_args)
            await admin_nodes.wizard_save_cb(self.query, self.bot, backend, self.state)
            backend.rollout_agent.assert_not_awaited()
            await admin_nodes.wizard_setup_agent_cb(self.query, self.bot, backend, self.state)
        args, kwargs = backend.create_node.call_args
        self.assertEqual(args[0], 123)
        self.assertEqual(kwargs['command_key'], 'create-key')
        self.assertEqual(args[1]['xray_transports'], ['tcp', 'xhttp'])
        self.assertEqual(args[1]['transport'], 'ssh')
        self.assertEqual(args[1]['ssh_target'], 'root@lv1.example.com')
        backend.rollout_agent.assert_awaited_once_with(123, 'lv1', 'ssh',
            ssh_target='root@lv1.example.com', command_key='rollout-key')

    async def test_switching_node_to_ssh_requires_and_saves_target(self):
        self.query.data = 'node_connection_set:lv1:ssh'
        backend = SimpleNamespace(request=AsyncMock(return_value={
            'key': 'lv1', 'transport': 'local', 'ssh_target': None,
            'desired_revision': 4, 'settings': {}}), edit_node=AsyncMock())
        message = SimpleNamespace(from_user=SimpleNamespace(id=123), text='root@lv1.example.com',
            chat=SimpleNamespace(id=123, type='private'), delete=AsyncMock())
        with patch.object(admin_nodes, 'render', new_callable=AsyncMock):
            await admin_nodes.node_connection_set_cb(self.query, self.bot, backend, self.state)
            backend.edit_node.assert_not_awaited()
            await admin_nodes.process_node_edit(message, self.bot, backend, self.state)
        backend.edit_node.assert_awaited_once()
        self.assertEqual(backend.edit_node.await_args.args[:4],
                         (123, 'lv1', 4, {'transport': 'ssh',
                                        'ssh_target': 'root@lv1.example.com'}))

    async def test_protocol_toggle_uses_a_valid_idempotency_key(self):
        backend = SimpleNamespace(request=AsyncMock(return_value={
            'protocols': ['awg', 'xray'], 'xray_transports': ['tcp'],
            'settings': {}, 'desired_revision': 3}), edit_node=AsyncMock())
        with patch.object(admin_nodes, 'show_node_protocols', new_callable=AsyncMock):
            await admin_nodes.toggle_node_feature(123, 123, 77, 'lv1', 'xhttp',
                                                  True, self.bot, backend, self.state)
        args, kwargs = backend.edit_node.await_args
        self.assertEqual(args[:3], (123, 'lv1', 3))
        self.assertEqual(args[3]['xray_transports'], ['tcp', 'xhttp'])
        UUID(kwargs['command_key'])

    async def test_apply_stays_in_settings_and_handles_backend_failure(self):
        node = {'key': 'lv1', 'title': 'Latvia', 'region': 'EU', 'flag': '🇱🇻',
                'protocols': ['awg'], 'xray_transports': [], 'transport': 'local',
                'ssh_target': None, 'settings': {}, 'desired_revision': 3,
                'applied_revision': 2}
        backend = SimpleNamespace(request=AsyncMock(return_value=node),
            node_services=AsyncMock(side_effect=BackendError('node_agent_unavailable', 503)),
            node_overview=AsyncMock(return_value={'state': 'changes_pending',
                'applied_revision': 2, 'desired_revision': 3,
                'ready': 0, 'access_total': 1, 'pending': 0,
                'failed': 0, 'attention': 1}),
            apply_node_settings=AsyncMock(side_effect=BackendError('agent_unavailable', 503)))
        with patch.object(admin_nodes, 'render', new_callable=AsyncMock) as render:
            await admin_nodes.show_node_settings(123, 123, 77, 'lv1',
                                                 self.bot, backend, self.state)
            settings_rows = render.call_args.args[3]
            self.assertTrue(any(button.callback_data.startswith('apply_node:')
                for row in settings_rows for button in row))
            await admin_nodes.show_admin_node(123, 123, 77, 'lv1',
                                              self.bot, backend, self.state)
            backend.node_overview.assert_awaited_once_with(123, 'lv1')
            card_rows = render.call_args.args[3]
            self.assertFalse(any(button.callback_data.startswith('apply_node:')
                for row in card_rows for button in row))
            result = await admin_nodes.apply_node(123, 123, 77, 'lv1',
                                                  self.bot, backend, self.state)
        self.assertFalse(result)

    async def test_probe_unavailable_shows_recovery_without_raw_rpc_error(self):
        backend = SimpleNamespace(node_runtime=AsyncMock(
            side_effect=BackendError('node_agent_unavailable', 503)))
        with patch.object(admin_nodes, 'render', new_callable=AsyncMock) as render:
            await admin_nodes.probe_node_cb(self.query, ProbeNodeCallback(node_key='lv1'),
                                            self.bot, backend, self.state)
        screen = render.call_args.args[2]
        self.assertIn('unreachable', ' '.join(screen.lines))
        self.assertNotIn('node_agent_unavailable', ' '.join(screen.lines))

    async def test_probe_shows_agent_version_before_protocol_runtime_exists(self):
        backend = SimpleNamespace(node_runtime=AsyncMock(return_value={
            'health_state': 'running', 'agent_version': '0.4.3-alpha.20',
            'runtime_version': '', 'xray_config_present': False, 'awg_config_present': False}))
        with patch.object(admin_nodes, 'render', new_callable=AsyncMock) as draw:
            await admin_nodes.probe_node_cb(self.query, ProbeNodeCallback(node_key='lv1'),
                                            self.bot, backend, self.state)
        lines = draw.call_args.args[2].lines
        self.assertIn('Agent version: 0.4.3-alpha.20', lines)
        self.assertIn('Runtime version: —', lines)

    async def test_node_diagnostics_displays_agent_status_without_paths(self):
        self.query.data = 'node_diagnostics:lv1'
        backend = SimpleNamespace(node_diagnostics=AsyncMock(return_value={
            'node_key': 'lv1', 'docker': 'ok', 'runtime_root': 'ok',
            'xray_config': 'ok', 'awg_config': 'missing',
            'runtime_version': '0.4.3'}))
        with patch.object(admin_nodes, 'render', new_callable=AsyncMock) as render:
            await admin_nodes.node_diagnostics_cb(self.query, self.bot, backend, self.state)
        screen = render.call_args.args[2]
        self.assertIn('Docker: ready', screen.lines)
        self.assertIn('AWG config: missing', screen.lines)
        self.assertNotIn('/opt/', ' '.join(screen.lines))
        backend.node_diagnostics.assert_awaited_once_with(123, 'lv1')

    async def test_node_diagnostics_unavailable_has_recovery(self):
        self.query.data = 'node_diagnostics:lv1'
        backend = SimpleNamespace(node_diagnostics=AsyncMock(
            side_effect=BackendError('node_agent_unavailable', 503)))
        with patch.object(admin_nodes, 'render', new_callable=AsyncMock) as render:
            await admin_nodes.node_diagnostics_cb(self.query, self.bot, backend, self.state)
        screen = render.call_args.args[2]
        self.assertIn('unreachable', ' '.join(screen.lines))
        self.assertNotIn('node_agent_unavailable', ' '.join(screen.lines))

    async def test_node_maintenance_translates_state_and_cleanup_phase(self):
        self.state_data['locale'] = 'ru'
        backend = SimpleNamespace(node_maintenance=AsyncMock(return_value={
            'status': 'draining', 'verification_target': 'root@lv1.example.com',
            'pending_tasks': 1, 'blocked_tasks': 0, 'cleanup_phase': 'prepared',
            'revocations_complete': False}))
        with patch.object(admin_nodes, 'render', new_callable=AsyncMock) as render:
            await admin_nodes.show_node_maintenance(123, 123, 77, 'lv1',
                                                    self.bot, backend, self.state)
        screen = render.call_args.args[2]
        self.assertIn('Состояние: отключается', screen.lines)
        self.assertIn('Очистка runtime: подготовлено', screen.lines)

    async def test_request_list_uses_backend_cursor_and_search_controls(self):
        backend = SimpleNamespace(pending_access_requests=AsyncMock(return_value={
            'items': [{'id': 'r1', 'account_id': 'account-one',
                'first_name': 'Alex', 'last_name': None, 'username': 'alex',
                'telegram_user_id': 456}], 'next_cursor': 'next'}))
        with patch.object(admin_requests, 'render', new_callable=AsyncMock) as render:
            await admin_requests.render_request_page(123, 123, 77, self.bot,
                backend, self.state, 0)
        screen, rows = render.call_args.args[2:4]
        callbacks = [button.callback_data for row in screen.fallback_rows(rows) for button in row]
        self.assertIn('request_page:1', callbacks)
        self.assertIn('request_search', callbacks)
        backend.pending_access_requests.assert_awaited_once_with(123,
            cursor=None, search=None, limit=10)

    async def test_request_notification_has_direct_approve_and_reject_actions(self):
        backend = SimpleNamespace(
            me=AsyncMock(return_value={
                'role': 'admin', 'status': 'approved', 'locale': 'en'}),
            access_request_policy=AsyncMock(return_value={'notify_requests': True}))
        with patch.dict('os.environ', {'ADMIN_IDS': '123'}), \
             patch.object(admin_requests, 'send_notice', new_callable=AsyncMock) as send_notice:
            await admin_requests.notify_admins(self.bot, backend, 'request-id')
        markup = send_notice.call_args.args[3]
        callbacks = [button.callback_data for row in markup.inline_keyboard
                     for button in row]
        self.assertEqual(len(callbacks), 3)
        self.assertTrue(any(value.startswith('notification_decide:') and 'approve' in value
                            for value in callbacks))
        self.assertTrue(any(value.startswith('notification_decide:') and 'reject' in value
                            for value in callbacks))

    async def test_request_notification_respects_admin_preference(self):
        backend = SimpleNamespace(
            me=AsyncMock(return_value={
                'role': 'admin', 'status': 'approved', 'locale': 'en'}),
            access_request_policy=AsyncMock(return_value={'notify_requests': False}))
        with patch.dict('os.environ', {'ADMIN_IDS': '123'}), \
             patch.object(admin_requests, 'send_notice', new_callable=AsyncMock) as send_notice:
            await admin_requests.notify_admins(self.bot, backend, 'request-id')
        send_notice.assert_not_awaited()

    async def test_notification_decision_shows_result_and_preserves_admin_screen(self):
        self.state_data['locale'] = 'en'
        for decision in ('approve', 'reject'):
            self.state_data.update(control_message_id=77, request_search='alice', request_page_index=2)
            self.query.message.message_id = 99
            self.bot.delete_message = AsyncMock()
            backend = SimpleNamespace(
                pending_access_request=AsyncMock(return_value={'locale': 'en'}),
                request=AsyncMock(side_effect=[{'account_id': 'member'}, {'telegram_user_id': None}]))
            with patch.object(admin_requests, 'render_request_page', new_callable=AsyncMock) as page, \
                 patch.object(admin_requests, 'render', new_callable=AsyncMock) as draw:
                await admin_requests.notification_decision_cb(self.query,
                    admin_requests.NotificationDecisionCallback(request_id='request', decision=decision),
                    self.bot, backend, self.state)
            self.bot.delete_message.assert_not_awaited()
            page.assert_not_awaited()
            rows = draw.call_args.args[3]
            callbacks = [b.callback_data for row in rows for b in row]
            self.assertIn('notification_close', callbacks)
            self.assertEqual(any(c.startswith('request_profile:') for c in callbacks), decision == 'approve')
            self.assertEqual(self.state_data['control_message_id'], 77)
            self.assertEqual(self.state_data['request_search'], 'alice')
            self.assertEqual(self.state_data['request_page_index'], 2)
            self.assertEqual(backend.request.call_args_list[0].kwargs['body'], {'decision': decision})

    async def test_stale_notification_is_deleted_but_backend_failure_keeps_it(self):
        self.state_data['locale'] = 'en'
        self.query.message.message_id = 99
        self.state_data['control_message_id'] = 77
        self.bot.delete_message = AsyncMock()
        backend = SimpleNamespace(pending_access_request=AsyncMock(
            side_effect=BackendError('resource_not_found', 404)), request=AsyncMock())
        with patch.object(admin_requests, 'render_request_page', new_callable=AsyncMock) as page:
            await admin_requests.apply_decision(self.query, 'request', 'approve',
                self.bot, backend, self.state, notification=True)
        page.assert_not_awaited()
        backend.request.assert_not_awaited()
        self.bot.delete_message.assert_awaited_once_with(123, 99)
        self.bot.delete_message.reset_mock()
        backend.pending_access_request.side_effect = BackendError('backend_unavailable', 503)
        with self.assertRaises(BackendError):
            await admin_requests.apply_decision(self.query, 'request', 'approve',
                self.bot, backend, self.state, notification=True)
        self.bot.delete_message.assert_not_awaited()
        self.assertEqual(self.state_data['control_message_id'], 77)

    async def test_notification_review_keeps_notification_callbacks_and_main_control(self):
        self.state_data['locale'] = 'en'
        self.query.message.message_id = 99
        self.state_data['control_message_id'] = 77
        item = {'id': 'request', 'account_id': 'member', 'username': 'alice', 'created_at': ''}
        backend = SimpleNamespace(pending_access_request=AsyncMock(return_value=item))
        async def render_notice(*args):
            await self.state.update_data(control_message_id=99)
        with patch.object(admin_requests, 'render', new_callable=AsyncMock,
                          side_effect=render_notice) as draw:
            await admin_requests.notification_review_cb(self.query,
                admin_requests.NotificationReviewCallback(request_id='request'),
                self.bot, backend, self.state)
        rows = draw.call_args.args[3]
        self.assertTrue(all(b.callback_data.startswith('notification_decide:') for b in rows[0]))
        self.assertEqual(rows[-1][0].callback_data, 'notification_close')
        self.assertEqual(self.state_data['control_message_id'], 77)

    async def test_deciding_last_request_returns_to_admin_menu(self):
        self.state_data['locale'] = 'en'
        backend = SimpleNamespace(
            pending_access_request=AsyncMock(return_value={
                'id': 'request-id', 'account_id': 'account-id', 'status': 'pending',
                'locale': 'en'}),
            pending_access_requests=AsyncMock(return_value={
                'items': [], 'next_cursor': None}),
            bot_title=AsyncMock(return_value={'title': 'Configured title'}),
            admin_overview=AsyncMock(side_effect=BackendError('backend_unavailable', 503)),
            request=AsyncMock(side_effect=[{'account_id': 'account-id'},
                BackendError('resource_not_found', 404)]))
        with patch.object(admin_requests, 'render', new_callable=AsyncMock), \
             patch.object(user, 'render', new_callable=AsyncMock) as render:
            await admin_requests.decide_cb(self.query,
                admin_requests.DecideCallback(request_id='request-id',
                                              decision='approve'),
                self.bot, backend, self.state)
        self.assertEqual(render.call_args.args[2].title, 'Admin panel · Configured title')
        self.assertEqual(render.call_args.args[2].lines, ())

    async def test_access_approval_replaces_requester_screen_and_menu_edits_same_message(self):
        from aiogram.fsm.context import FSMContext
        from aiogram.fsm.storage.base import StorageKey
        from aiogram.fsm.storage.memory import MemoryStorage
        storage = MemoryStorage()
        admin_state = FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=123, user_id=123))
        member_state = FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=456, user_id=456))
        await admin_state.update_data(locale='en', control_message_id=77)
        await member_state.update_data(locale='ru', control_message_id=88)
        backend = SimpleNamespace(
            pending_access_request=AsyncMock(return_value={'locale': 'ru'}),
            request=AsyncMock(side_effect=[{'account_id': 'member'}, {'telegram_user_id': 456}]),
            me=AsyncMock(return_value={'role': 'member', 'status': 'approved'}),
            bot_title=AsyncMock(return_value={'title': 'Node Plane'}))
        self.bot.edit_message_text = AsyncMock()
        self.bot.send_rich_message = AsyncMock(return_value=SimpleNamespace(message_id=99))
        self.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=100))
        with patch.object(admin_requests, 'render_request_page', new_callable=AsyncMock):
            await admin_requests.apply_decision(self.query, 'request', 'approve',
                                               self.bot, backend, admin_state)
        edited = self.bot.edit_message_text.call_args.kwargs
        self.assertEqual((edited['chat_id'], edited['message_id']), (456, 88))
        blocks = edited['rich_message'].blocks
        self.assertEqual(blocks[1].text, 'Ваш запрос доступа одобрен.')
        self.assertEqual(blocks[-1].buttons[0].text, 'В меню')
        self.assertEqual(blocks[-1].buttons[0].callback_data, user.HomeCallback().pack())
        self.assertEqual((await admin_state.get_data())['control_message_id'], 77)
        self.assertEqual((await member_state.get_data())['control_message_id'], 88)
        query = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(
            chat=SimpleNamespace(id=456), message_id=88),
            from_user=SimpleNamespace(id=456, language_code='ru'))
        await user.home_cb(query, self.bot, backend, member_state)
        self.assertEqual(self.bot.edit_message_text.call_args.kwargs['message_id'], 88)
        self.bot.send_rich_message.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()
        await storage.close()

    async def test_access_decision_without_existing_screen_records_one_new_control_message(self):
        from aiogram.fsm.context import FSMContext
        from aiogram.fsm.storage.base import StorageKey
        from aiogram.fsm.storage.memory import MemoryStorage
        storage = MemoryStorage()
        admin_state = FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=123, user_id=123))
        member_state = FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=456, user_id=456))
        await admin_state.update_data(locale='en')
        backend = SimpleNamespace(
            pending_access_request=AsyncMock(return_value={'locale': 'en'}),
            request=AsyncMock(side_effect=[{'account_id': 'member'}, {'telegram_user_id': 456}]))
        self.bot.send_rich_message = AsyncMock(return_value=SimpleNamespace(message_id=99))
        self.bot.send_message = AsyncMock()
        with patch.object(admin_requests, 'render_request_page', new_callable=AsyncMock):
            await admin_requests.apply_decision(self.query, 'request', 'reject',
                                               self.bot, backend, admin_state)
        self.bot.send_rich_message.assert_awaited_once()
        self.assertEqual((await member_state.get_data())['control_message_id'], 99)
        blocks = self.bot.send_rich_message.call_args.kwargs['rich_message'].blocks
        self.assertEqual(blocks[-1].buttons[0].text, 'To menu')
        await storage.close()

    async def test_failed_node_cleanup_has_recoverable_screen(self):
        from telegram_client.routers.callbacks import CleanupStepCallback
        backend = SimpleNamespace(cleanup_node_step=AsyncMock(
            side_effect=BackendError('node_agent_unavailable', 503)))
        with patch.object(admin_nodes, 'render', new_callable=AsyncMock) as render:
            await admin_nodes.cleanup_step_cb(self.query,
                CleanupStepCallback(node_key='lv1', expected_phase='prepared'),
                self.bot, backend, self.state)
        screen = render.call_args.args[2]
        callbacks = [button.callback_data for row in render.call_args.args[3]
                     for button in row]
        self.assertNotIn('node_agent_unavailable', ' '.join(screen.lines))
        self.assertTrue(any(value.startswith('node_maintenance:') for value in callbacks))

    async def test_agent_setup_success_links_to_settings_without_dead_runtime_button(self):
        backend = SimpleNamespace(agent_rollout=AsyncMock(return_value={
            'id': 'task1', 'node_key': 'lv1', 'status': 'succeeded'}))
        with patch.object(admin_nodes, 'render', new_callable=AsyncMock) as render:
            await admin_nodes.show_rollout_status(123, 123, 77, 'task1',
                                                  self.bot, backend, self.state)
        callbacks = [button.callback_data for row in render.call_args.args[3]
                     for button in row]
        self.assertTrue(any(value.startswith('node_settings:') for value in callbacks))
        self.assertFalse(any(value.startswith('refresh_runtime:') for value in callbacks))

    async def test_install_menu_checks_docker_and_reusable_configs(self):
        from telegram_client.routers import admin_node_tools
        node = {'key': 'lv1', 'transport': 'local', 'ssh_target': None, 'protocols': ['awg', 'xray']}
        facts = {'docker': False, 'awg_config_valid': False, 'xray_config_valid': False}
        backend = SimpleNamespace(request=AsyncMock(return_value=node),
            node_overview=AsyncMock(return_value={'settings_complete': True}),
            node_services=AsyncMock(return_value=facts))
        with patch.object(admin_node_tools, 'render', new_callable=AsyncMock) as render:
            async def buttons():
                await admin_node_tools.show_install(123, 123, 77, 'lv1', self.bot, backend, self.state)
                return [b.callback_data for row in render.call_args.args[3] for b in row]
            self.assertIn('node_action:install_docker:lv1', await buttons())
            facts['docker'] = True
            self.assertIn('node_action:bootstrap:lv1', await buttons())
            self.assertNotIn('node_action:reinstall_keep:lv1', await buttons())
            facts['awg_config_valid'] = True
            self.assertIn('node_action:reinstall_clean:lv1', await buttons())
            self.assertNotIn('node_action:reinstall_keep:lv1', await buttons())
            facts['xray_config_valid'] = True
            self.assertIn('node_action:reinstall_keep:lv1', await buttons())
            backend.node_overview.return_value = {'settings_complete': False}
            self.assertFalse(any(v.startswith('node_action:') for v in await buttons()))

    async def test_node_wizard_navigation_has_back_and_paired_forward_buttons(self):
        self.state_data['wizard_data'] = {'key': 'lv1', 'title': 'Latvia',
            'region': 'EU', 'flag': '🇱🇻', 'transport': 'ssh',
            'ssh_target': 'root@lv1.example.com', 'public_host': 'lv1.example.com',
            'protocols': ['awg']}
        with patch.object(admin_nodes, 'render', new_callable=AsyncMock) as render:
            await admin_nodes.render_wizard_step(123, self.bot, self.state, 'key', 77)
            self.assertEqual([[button.callback_data for button in row]
                for row in render.call_args.args[3]], [['admin_nodes']])
            await admin_nodes.render_wizard_step(123, self.bot, self.state, 'flag', 77)
            self.assertEqual([[button.callback_data for button in row]
                for row in render.call_args.args[3]],
                [['wizard_back:region', 'wizard_skip_flag']])
            await admin_nodes.render_wizard_step(123, self.bot, self.state, 'public_host', 77)
            self.assertEqual(render.call_args.args[3][0][0].callback_data,
                             'wizard_back:target')
            await admin_nodes.render_wizard_protocols(123, self.bot, self.state, 77)
            self.assertEqual([button.callback_data for button in
                render.call_args.args[3][-1]],
                ['wizard_back:public_host', 'wizard_proto:done'])
            await admin_nodes.render_wizard_summary(123, self.bot, self.state, 77)
            self.assertEqual([button.callback_data for button in
                render.call_args.args[3][0]], ['wizard_proto:back', 'wizard_save'])

    async def test_profile_wizard_back_keeps_draft_and_next_opens_review(self):
        backend = SimpleNamespace(admin_nodes=AsyncMock(return_value={'items': [
            {'key': 'n1', 'title': 'Node 1', 'protocols': ['awg', 'xray']},
            {'key': 'n2', 'title': 'Node 2', 'protocols': ['xray']}]}))
        with patch.object(admin_profiles, 'render', new_callable=AsyncMock) as render:
            await admin_profiles.new_profile_cb(self.query,
                NewProfileCallback(account_id='account1'), self.bot, self.state)
            self.assertTrue(render.call_args.args[3][0][0].callback_data.startswith('account:'))
            self.state_data.update(draft_profile_name='Alice', draft_grants=[
                {'node_key': 'n1', 'protocol': 'awg'},
                {'node_key': 'n2', 'protocol': 'xray'}])
            await admin_profiles.show_create_nodes(123, 123, 77,
                                                   self.bot, backend, self.state)
            self.assertEqual([button.callback_data for button in
                render.call_args.args[3][-1]],
                ['profile_draft_name', 'profile_draft_review'])
            self.query.data = 'profile_draft_node:0'
            await admin_profiles.draft_node_cb(self.query, self.bot, self.state)
            self.assertEqual([button.callback_data for button in
                render.call_args.args[3][-1]],
                ['profile_draft_nodes', 'profile_draft_review'])
            self.query.data = 'profile_draft_toggle:xray'
            await admin_profiles.draft_toggle_cb(self.query, self.bot, self.state)
            self.query.data = 'profile_draft_nodes'
            await admin_profiles.draft_nodes_cb(self.query, self.bot,
                                               backend, self.state)
            self.assertIn({'node_key': 'n1', 'protocol': 'xray'},
                          self.state_data['draft_grants'])
            self.assertIsNone(self.state_data['draft_node_key'])
            self.query.data = 'profile_draft_review'
            await admin_profiles.draft_review_cb(self.query, self.bot, self.state)
            self.assertEqual([button.callback_data for button in
                render.call_args.args[3][0]],
                ['profile_draft_nodes', 'profile_draft_save'])
            self.assertIn('Node 1', render.call_args.args[2].plain())

    async def test_profile_wizard_first_back_clears_draft_at_account_card(self):
        self.state_data['locale'] = 'ru'
        backend = SimpleNamespace(request=AsyncMock(return_value={
            'id': 'account1', 'telegram_user_id': 123,
            'status': 'approved', 'role': 'admin'}))
        with patch.object(admin_profiles, 'render', new_callable=AsyncMock) as render:
            await admin_profiles.new_profile_cb(self.query,
                NewProfileCallback(account_id='account1'), self.bot, self.state)
            self.state_data['draft_profile_name'] = 'Alice'
            await admin_profiles.account_cb(self.query,
                admin_profiles.AccountCallback(account_id='account1'),
                self.bot, backend, self.state)
        self.assertEqual(self.state_data, {'locale': 'ru'})
        self.assertTrue(render.call_args.args[3][-1][0].callback_data.startswith('accounts'))
