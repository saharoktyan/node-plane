"""Profile Rich UI, draft isolation, paging and authenticated mutations."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.backend import BackendError
from telegram_client.routers import admin_profiles as profiles
from telegram_client.routers.callbacks import AdminProfilesCallback, AddGrantCallback


class ProfileRichTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.data = {'locale': 'en', 'admin_profile_search': 'alice',
                     'admin_profile_page': 1, 'admin_profile_cursors': [None, 'cursor']}
        async def get_data():
            return dict(self.data)
        async def update_data(**values):
            self.data.update(values)
        self.state = SimpleNamespace(get_data=get_data, update_data=update_data,
                                     set_state=AsyncMock())
        self.query = SimpleNamespace(data='', answer=AsyncMock(),
            from_user=SimpleNamespace(id=123),
            message=SimpleNamespace(message_id=77, chat=SimpleNamespace(id=123)))
        self.bot = SimpleNamespace()
        self.profile = {'id': 'p1', 'display_name': 'Alice', 'frozen': False,
                        'desired_revision': 7, 'owner_account_id': None, 'expires_at': None}
        self.nodes = [{'key': f'n{i:02}', 'title': f'Node {i:02}', 'region': 'Latvia' if i < 10 else 'Germany',
                       'flag': '🇱🇻' if i < 10 else '🇩🇪', 'protocols': ['awg', 'xray']}
                      for i in range(23)]
        self.grants = [{'node_key': node['key'], 'protocol': 'awg'} for node in self.nodes]
        self.backend = SimpleNamespace(
            request=AsyncMock(return_value=self.profile),
            profile_grants=AsyncMock(return_value={'items': self.grants}),
            admin_nodes=AsyncMock(return_value={'items': self.nodes}),
            profile_operation=AsyncMock(return_value={'status': 'blocked',
                'tasks': [{'node_key': 'n00', 'protocol': 'awg', 'status': 'blocked'}]}),
            edit_profile=AsyncMock(), replace_grants=AsyncMock())

    async def test_list_retains_search_page_with_embedded_record_actions(self):
        self.backend.admin_profiles = AsyncMock(return_value={'items': [self.profile], 'next_cursor': 'next'})
        with patch.object(profiles, 'render', new_callable=AsyncMock) as draw:
            await profiles.admin_profiles_cb(self.query, self.bot, self.backend, self.state)
        self.backend.admin_profiles.assert_awaited_once_with(123, cursor='cursor', search='alice')
        screen, rows = draw.call_args.args[2:4]
        self.assertTrue(screen.embedded_buttons)
        self.assertIn('Search: alice', screen.plain())
        self.assertEqual(screen.sections[1].title, '')
        self.assertEqual(screen.sections[1].rows[0][0].text, 'Alice · Active')
        self.assertEqual([b.size for b in screen.rich(rows).blocks if b.type == 'heading'], [1])
        self.assertEqual([b.text for b in rows[0]], ['←', '→'])
        self.assertEqual(rows[-1][0].callback_data, 'admin_menu')
        self.assertEqual([b.type for b in screen.rich(rows).blocks[-2:]], ['divider', 'buttons'])

    async def test_landing_card_contains_only_summary_primary_actions_and_management(self):
        for locale in ('ru', 'en'):
            self.data['locale'] = locale
            with patch.object(profiles, 'render', new_callable=AsyncMock) as draw:
                await profiles.show_admin_profile(123, 123, 77, 'p1', self.bot, self.backend, self.state)
            screen, rows = draw.call_args.args[2:4]
            self.assertEqual(screen.title, 'Alice')
            self.assertEqual(screen.sections, ())
            self.assertIn('23', ' '.join(screen.lines))
            self.assertNotIn('p1', ' '.join(screen.lines))
            self.assertEqual(len(rows[0]), 2)
            callbacks = [b.callback_data for row in rows for b in row]
            self.assertIn('grant_nodes:p1', callbacks)
            self.assertIn('admin_profile_edit:p1', callbacks)
            self.assertIn('prof_manage:p1', callbacks)
            for prefix in ('prof_del:', 'prof_op:', 'prof_state:', 'admin_profile_rename:'):
                self.assertFalse(any(c.startswith(prefix) for c in callbacks))
            self.assertEqual(rows[-1][0].callback_data, AdminProfilesCallback().pack())
            self.assertEqual([b.type for b in screen.rich(rows).blocks[-2:]], ['divider', 'buttons'])
        self.backend.admin_nodes.assert_not_awaited()

    async def test_access_editor_reads_all_registry_pages_and_preserves_server_paging(self):
        self.backend.admin_nodes.side_effect = [
            {'items': self.nodes[:10], 'next_cursor': 'next'}, {'items': self.nodes[10:]}]
        self.query.data = 'prof_access_page:p1:2'
        with patch.object(profiles, 'render', new_callable=AsyncMock) as draw:
            await profiles.profile_access_page_cb(self.query, self.bot, self.backend, self.state)
        screen = draw.call_args.args[2]
        groups = screen.sections[1:]
        self.assertEqual(sum(len(g.sections) for g in groups), 3)
        self.assertTrue(any('Node 09' in node.title for g in groups for node in g.sections))
        self.assertEqual(self.data['grant_nodes_page'], 2)
        self.assertEqual(self.backend.admin_nodes.await_count, 2)

    async def test_operation_details_are_only_reachable_through_management_and_technical(self):
        self.profile['owner_account_id'] = 'a1'
        with patch.object(profiles, 'render', new_callable=AsyncMock) as draw:
            await profiles.show_profile_management(123, 123, 77, 'p1', self.bot, self.backend, self.state)
            screen, rows = draw.call_args.args[2:4]
            callbacks = [b.callback_data for row in rows for b in row]
            self.assertIn('prof_role:p1', callbacks)
            self.assertIn('prof_del:p1', callbacks)
            self.assertIn('prof_tech:p1', callbacks)
            self.assertNotIn('prof_op:p1', callbacks)
            self.assertEqual(rows[-1][0].callback_data, 'admin_profile:p1')
            self.query.data = 'prof_tech:p1'
            await profiles.profile_technical_cb(self.query, self.bot, self.backend, self.state)
            rows = draw.call_args.args[3]
            self.assertEqual(rows[0][0].callback_data, 'prof_op:p1')
            self.assertEqual(rows[-1][0].callback_data, 'prof_manage:p1')
            self.query.data = 'prof_op:p1'
            await profiles.profile_operation_cb(self.query, self.bot, self.backend, self.state)
        self.assertEqual(draw.call_args.args[3][-1][0].callback_data, 'prof_tech:p1')

    async def test_grant_editor_pages_preserve_draft_until_explicit_save(self):
        with patch.object(profiles, 'render', new_callable=AsyncMock) as draw:
            await profiles.show_grant_nodes(123, 123, 77, 'p1', self.bot, self.backend, self.state)
            self.assertTrue(draw.call_args.args[2].embedded_buttons)
            self.query.data = 'grant_page:p1:1'
            await profiles.grant_page_cb(self.query, self.bot, self.backend, self.state)
            await profiles.add_grant_cb(self.query,
                AddGrantCallback(profile_id='p1', node_key='n01', protocol='xray'),
                self.bot, self.backend, self.state)
        self.assertEqual(self.data['grant_nodes_page'], 1)
        self.assertIn({'node_key': 'n01', 'protocol': 'xray'}, self.data['draft_grants'])
        self.backend.replace_grants.assert_not_awaited()
        self.profile['desired_revision'] = 99
        self.query.data = 'admin_profile_grants_save:p1'
        with patch.object(profiles, 'show_admin_profile', new_callable=AsyncMock):
            await profiles.save_grants_cb(self.query, self.bot, self.backend, self.state)
        self.assertEqual(self.backend.replace_grants.call_args.args[2], 7)

    async def test_revision_conflict_preserves_unsaved_grants_for_review(self):
        await profiles._ensure_grant_draft(self.backend, 123, 'p1', self.state)
        self.data['draft_grants'].append({'node_key': 'n00', 'protocol': 'xray'})
        self.backend.replace_grants.side_effect = BackendError('revision_conflict', 412)
        self.query.data = 'admin_profile_grants_save:p1'
        with patch.object(profiles, 'show_admin_profile', new_callable=AsyncMock) as card:
            await profiles.save_grants_cb(self.query, self.bot, self.backend, self.state)
        card.assert_not_awaited()
        self.assertEqual(self.data['edit_profile_id'], 'p1')
        self.assertIn({'node_key': 'n00', 'protocol': 'xray'}, self.data['draft_grants'])
        self.assertTrue(self.query.answer.call_args.kwargs['show_alert'])

    async def test_selected_active_button_is_idempotent_and_freeze_sets_explicit_value(self):
        with patch.object(profiles, 'show_profile_edit_menu', new_callable=AsyncMock):
            self.query.data = 'prof_state:p1:active'
            await profiles.set_profile_state_cb(self.query, self.bot, self.backend, self.state)
            self.backend.edit_profile.assert_not_awaited()
            self.query.data = 'prof_state:p1:frozen'
            await profiles.set_profile_state_cb(self.query, self.bot, self.backend, self.state)
        self.backend.edit_profile.assert_awaited_once_with(123, 'p1', 7, {'frozen': True})

    async def test_deleting_profile_exposes_no_mutations(self):
        self.profile['deleting'] = True
        with patch.object(profiles, 'render', new_callable=AsyncMock) as draw:
            await profiles.show_admin_profile(123, 123, 77, 'p1', self.bot, self.backend, self.state)
        screen, rows = draw.call_args.args[2:4]
        callbacks = [b.callback_data for row in screen.fallback_rows(rows) for b in row]
        self.assertFalse(any(c.startswith(('prof_state:', 'prof_del:', 'admin_profile_rename:', 'grant_nodes:'))
                             for c in callbacks))

    async def test_edit_link_opens_dedicated_editor_and_discards_old_grant_draft(self):
        self.data.update(edit_profile_id='p1', draft_grants=[{'node_key': 'n00', 'protocol': 'xray'}])
        with patch.object(profiles, 'render', new_callable=AsyncMock) as draw:
            await profiles.show_profile_edit_menu(123, 123, 77, 'p1', self.bot, self.backend, self.state)
        screen, rows = draw.call_args.args[2:4]
        self.assertEqual(screen.title, 'Edit')
        callbacks = [b.callback_data for row in screen.fallback_rows(rows) for b in row]
        self.assertIn('admin_profile_rename:p1', callbacks)
        self.assertIn('prof_expiry:p1', callbacks)
        self.assertIn('prof_state:p1:frozen', callbacks)
        self.assertNotIn('prof_del:p1', callbacks)
        self.assertEqual(rows[-1][0].callback_data, 'admin_profile:p1')
        self.assertIsNone(self.data['draft_grants'])

    async def test_repeating_node_cursor_is_rejected(self):
        self.backend.admin_nodes.return_value = {'items': [], 'next_cursor': 'same'}
        with self.assertRaises(BackendError):
            await profiles._all_nodes(self.backend, 123)
        self.assertEqual(self.backend.admin_nodes.await_count, 2)

    async def test_region_bulk_includes_other_pages_and_new_nodes_on_next_explicit_press(self):
        with patch.object(profiles, 'render', new_callable=AsyncMock) as draw:
            await profiles.show_grant_nodes(123, 123, 77, 'p1', self.bot, self.backend, self.state)
            region = next(s for s in draw.call_args.args[2].sections if s.title == 'Germany')
            self.query.data = region.heading_rows[0][0].callback_data
            # A server appeared after the initial screen was drawn.
            self.nodes.append({'key': 'new', 'title': 'New', 'region': 'Germany',
                               'flag': '🇩🇪', 'enabled': True, 'protocols': ['awg', 'xray']})
            await profiles.grant_bulk_cb(self.query, self.bot, self.backend, self.state)
            granted = {(g['node_key'], g['protocol']) for g in self.data['draft_grants']}
            self.assertIn(('n22', 'xray'), granted)  # Not on the displayed page.
            self.assertIn(('new', 'awg'), granted)
            self.assertNotIn(('n00', 'xray'), granted)  # Other region unchanged.
            region = next(s for s in draw.call_args.args[2].sections if s.title == 'Germany')
            self.query.data = region.heading_rows[0][1].callback_data
            await profiles.grant_bulk_cb(self.query, self.bot, self.backend, self.state)
        self.assertEqual({g['node_key'] for g in self.data['draft_grants']},
                         {f'n{i:02}' for i in range(10)})
        self.backend.replace_grants.assert_not_awaited()

    async def test_all_bulk_is_additive_idempotent_skips_disabled_and_revokes_unknown_nodes(self):
        self.nodes.append({'key': 'disabled', 'title': 'Disabled', 'region': 'Germany',
                           'enabled': False, 'protocols': ['awg', 'xray']})
        self.grants.append({'node_key': 'removed', 'protocol': 'awg'})
        with patch.object(profiles, 'render', new_callable=AsyncMock) as draw:
            await profiles.show_grant_nodes(123, 123, 77, 'p1', self.bot, self.backend, self.state)
            self.query.data = 'grant_bulk:p1:add:all'
            await profiles.grant_bulk_cb(self.query, self.bot, self.backend, self.state)
            first = list(self.data['draft_grants'])
            await profiles.grant_bulk_cb(self.query, self.bot, self.backend, self.state)
            self.assertEqual(self.data['draft_grants'], first)
            self.assertEqual(len(first), 47)
            self.assertFalse(any(g['node_key'] == 'disabled' for g in first))
            self.assertIn({'node_key': 'removed', 'protocol': 'awg'}, first)
            self.query.data = 'grant_bulk:p1:del:all'
            await profiles.grant_bulk_cb(self.query, self.bot, self.backend, self.state)
        self.assertEqual(self.data['draft_grants'], [])
        self.backend.replace_grants.assert_not_awaited()

    async def test_region_tokens_are_short_bound_to_raw_unicode_names_and_stale_tokens_do_not_mutate(self):
        self.nodes[0]['region'] = 'Очень длинное название региона ' * 5
        profile_id = '00000000-0000-0000-0000-000000000001'
        self.profile['id'] = profile_id
        await profiles._ensure_grant_draft(self.backend, 123, profile_id, self.state)
        self.data['grant_nodes_page'] = 2
        with patch.object(profiles, 'render', new_callable=AsyncMock) as draw:
            await profiles.show_grant_nodes(123, 123, 77, profile_id, self.bot, self.backend, self.state)
            screen, rows = draw.call_args.args[2:4]
            self.assertTrue(all(len(b.callback_data.encode()) <= 64
                for row in screen.fallback_rows(rows) for b in row))
            scope = next(token for token, region in self.data['grant_bulk_regions'].items()
                         if region.startswith('Очень'))
            original = list(self.data['draft_grants'])
            await profiles.show_grant_nodes(123, 123, 77, profile_id, self.bot, self.backend, self.state)
            self.query.data = f'grant_bulk:{profile_id}:del:{scope}'
            await profiles.grant_bulk_cb(self.query, self.bot, self.backend, self.state)
        self.assertEqual(self.data['draft_grants'], original)

    async def test_create_wizard_bulk_changes_only_creation_draft_and_keeps_navigation_row(self):
        self.data.update(profile_account_id='account', draft_profile_name='Alice', draft_grants=[])
        with patch.object(profiles, 'render', new_callable=AsyncMock) as draw:
            await profiles.show_create_nodes(123, 123, 77, self.bot, self.backend, self.state)
            self.query.data = draw.call_args.args[2].sections[0].heading_rows[0][0].callback_data
            await profiles.draft_bulk_cb(self.query, self.bot, self.backend, self.state)
            self.assertEqual(len(self.data['draft_grants']), 46)
            self.assertEqual([b.callback_data for b in draw.call_args.args[3][-1]],
                             ['profile_draft_name', 'profile_draft_review'])
            region = next(s for s in draw.call_args.args[2].sections if s.title == 'Germany')
            self.query.data = region.heading_rows[0][1].callback_data
            await profiles.draft_bulk_cb(self.query, self.bot, self.backend, self.state)
        self.assertEqual(len(self.data['draft_grants']), 20)
        self.backend.replace_grants.assert_not_awaited()

    async def test_deleted_owner_menu_cache_is_revoked_and_existing_message_reused(self):
        from aiogram.fsm.context import FSMContext
        from aiogram.fsm.storage.base import StorageKey
        from aiogram.fsm.storage.memory import MemoryStorage
        from telegram_client.routers import user
        storage = MemoryStorage()
        admin = FSMContext(storage, StorageKey(bot_id=1, chat_id=123, user_id=123))
        member = FSMContext(storage, StorageKey(bot_id=1, chat_id=456, user_id=456))
        await member.update_data(locale='ru', control_message_id=88,
            home_presentation={'user_id': 456, 'account': {'role': 'member', 'status': 'approved'}},
            issuance_poll_token='old')
        await member.set_state('old_wizard')
        account = {'telegram_user_id': 456, 'role': 'member', 'status': 'pending'}
        backend = SimpleNamespace(request=AsyncMock(return_value=account))
        with patch.object(user, 'show_home', new_callable=AsyncMock) as home:
            await profiles.refresh_deleted_owner({'profile': {'owner_account_id': 'member'}},
                123, self.bot, backend, admin)
        data = await member.get_data()
        self.assertIsNone(data['home_presentation'])
        self.assertIsNone(data['issuance_poll_token'])
        self.assertIsNone(await member.get_state())
        self.assertEqual(data['control_message_id'], 88)
        self.assertEqual(data['locale'], 'ru')
        self.assertEqual(home.call_args.args[:2], (456, 456))
        self.assertEqual(home.call_args.kwargs['account'], account)
        backend.request.return_value = {**account, 'status': 'approved'}
        home.reset_mock()
        with patch.object(user, 'show_home', home):
            await profiles.refresh_deleted_owner({'profile': {'owner_account_id': 'member'}},
                123, self.bot, backend, admin)
        home.assert_not_awaited()
        await storage.close()

    async def test_successful_sync_does_not_add_routine_operation_information_to_card(self):
        self.backend.profile_operation.return_value = {'status': 'succeeded', 'tasks': []}
        self.profile['expires_at'] = '2099-01-01T12:00:00+00:00'
        with patch.object(profiles, 'render', new_callable=AsyncMock) as draw:
            await profiles.show_admin_profile(123, 123, 77, 'p1', self.bot, self.backend, self.state)
        screen = draw.call_args.args[2]
        self.assertEqual(len(screen.lines), 3)
        self.assertIn('2099-01-01 12:00 UTC', screen.lines[1])
        self.assertNotIn('operation', screen.plain().lower())

    async def test_expiry_presets_capture_revision_and_save_once_then_return_to_edit(self):
        self.query.data = 'prof_expiry:p1'
        for locale in ('ru', 'en'):
            self.data['locale'] = locale
            with patch.object(profiles, 'render', new_callable=AsyncMock) as draw:
                await profiles.profile_expiry_cb(self.query, self.bot, self.backend, self.state)
            rows = draw.call_args.args[3]
            self.assertEqual([len(row) for row in rows], [3, 2, 1])
            self.assertEqual(rows[-1][0].callback_data, 'admin_profile_edit:p1')
        draft = self.data['profile_expiry']
        self.profile['desired_revision'] = 10
        self.query.data = 'prof_exp_set:' + draft['nonce'] + ':30'
        with patch.object(profiles, 'show_profile_edit_menu', new_callable=AsyncMock) as editor:
            await profiles.profile_expiry_set_cb(self.query, self.bot, self.backend, self.state)
        self.backend.edit_profile.assert_awaited_once_with(123, 'p1', 7,
            {'expires_at': draft['values']['30']}, command_key=draft['command_key'])
        self.assertEqual(editor.call_args.args[3], 'p1')
        self.data['profile_expiry'] = None
        self.backend.edit_profile.reset_mock()
        await profiles.profile_expiry_set_cb(self.query, self.bot, self.backend, self.state)
        self.backend.edit_profile.assert_not_awaited()

    async def test_no_expiry_saves_null_and_revision_conflict_preserves_form(self):
        self.query.data = 'prof_expiry:p1'
        with patch.object(profiles, 'render', new_callable=AsyncMock):
            await profiles.profile_expiry_cb(self.query, self.bot, self.backend, self.state)
        draft = self.data['profile_expiry']
        self.query.data = 'prof_exp_set:' + draft['nonce'] + ':none'
        self.backend.edit_profile.side_effect = BackendError('revision_conflict', 412)
        with patch.object(profiles, 'show_profile_edit_menu', new_callable=AsyncMock) as editor:
            await profiles.profile_expiry_set_cb(self.query, self.bot, self.backend, self.state)
        editor.assert_not_awaited()
        self.assertEqual(self.backend.edit_profile.call_args.args[3], {'expires_at': None})
        self.assertEqual(self.data['profile_expiry'], draft)
        self.assertTrue(self.query.answer.call_args.kwargs['show_alert'])

    async def test_custom_expiry_date_and_one_step_back(self):
        self.query.data = 'prof_expiry:p1'
        with patch.object(profiles, 'render', new_callable=AsyncMock):
            await profiles.profile_expiry_cb(self.query, self.bot, self.backend, self.state)
        draft = self.data['profile_expiry']
        self.query.data = 'prof_exp_date:' + draft['nonce']
        with patch.object(profiles, 'render', new_callable=AsyncMock) as draw:
            await profiles.profile_expiry_date_cb(self.query, self.bot, self.state)
        self.assertEqual(draw.call_args.args[3][-1][0].callback_data, 'prof_expiry:p1')
        message = SimpleNamespace(text='2099-12-31', delete=AsyncMock(),
            from_user=SimpleNamespace(id=123), chat=SimpleNamespace(id=123, type='private'))
        with patch.object(profiles, 'show_profile_edit_menu', new_callable=AsyncMock):
            await profiles.profile_expiry_message(message, self.bot, self.backend, self.state)
        self.backend.edit_profile.assert_awaited_once_with(123, 'p1', 7,
            {'expires_at': '2099-12-31T23:59:59+00:00'}, command_key=draft['command_key'])

    async def test_invalid_expiry_dates_do_not_mutate_backend(self):
        self.query.data = 'prof_expiry:p1'
        with patch.object(profiles, 'render', new_callable=AsyncMock):
            await profiles.profile_expiry_cb(self.query, self.bot, self.backend, self.state)
            for value in ('2000-01-01', '2099-02-30', 'tomorrow', '2099-1-2'):
                message = SimpleNamespace(text=value, delete=AsyncMock(),
                    from_user=SimpleNamespace(id=123), chat=SimpleNamespace(id=123, type='private'))
                await profiles.profile_expiry_message(message, self.bot, self.backend, self.state)
        self.backend.edit_profile.assert_not_awaited()

    async def test_delete_confirmation_back_returns_to_management(self):
        self.query.data = 'prof_del:p1'
        with patch.object(profiles, 'render', new_callable=AsyncMock) as draw:
            await profiles.delete_profile_confirm_cb(self.query, self.bot, self.backend, self.state)
        self.assertEqual(draw.call_args.args[3][-1][0].callback_data, 'prof_manage:p1')
