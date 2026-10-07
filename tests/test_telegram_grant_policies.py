from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

from telegram_client import access_policy as policy_ui
from telegram_client.backend import BackendError
from telegram_client.routers import admin_profiles as profiles, admin_nodes as nodes
from tests import test_telegram_admin_profiles_rich as profile_fixture
from tests import test_telegram_admin_nodes_rich as node_fixture


class TelegramGrantPolicyTests(IsolatedAsyncioTestCase):
    def setUp(self):
        profile_fixture.ProfileRichTests.setUp(self)
        self.data['control_message_id'] = 77
        self.nodes = [dict(node, enabled=True, region_id='eu', region='Europe') for node in self.nodes[:2]]
        self.grants = []
        self.backend.admin_nodes.return_value = {'items': self.nodes}
        self.regions = [{'id': 'eu', 'title': 'Europe'}, {'id': 'asia', 'title': 'Asia'}]
        async def request(method, path, **kwargs):
            return {'items': self.regions} if path.startswith('/api/v1/regions?') else self.profile
        self.backend.request.side_effect = request

    async def toggle_future(self, callback):
        self.query.data = callback
        await profiles.future_toggle_cb(self.query, self.bot, self.backend, self.state)

    async def test_rules_edit_only_draft_and_save_retains_identity_on_uncertain_retry(self):
        for locale in ('en', 'ru'):
            self.data['locale'] = locale
            with patch.object(profiles, 'render', new_callable=AsyncMock) as draw:
                await profiles.show_future_rules(123, 123, 77, self.bot, self.backend, self.state, 'edit', 'p1')
                callback = draw.call_args.args[2].sections[0].rows[0][0].callback_data
                await self.toggle_future(callback)
                screen, rows = draw.call_args.args[2:4]
                self.assertEqual(screen.sections[0].rows[0][0].style, 'primary')
                self.assertEqual(self.data['draft_grants'], [])
                self.assertEqual(self.data['original_rules'], [])
                self.assertTrue(policy_ui.changed(self.data))
                self.assertTrue(all(len(b.callback_data.encode()) <= 64 for row in screen.fallback_rows(rows) for b in row))
                await profiles.show_grant_nodes(123, 123, 77, 'p1', self.bot, self.backend, self.state)
                self.assertIn('admin_profile_grants_save:p1', [b.callback_data for row in draw.call_args.args[3] for b in row])
            self.backend.replace_access_policy.assert_not_awaited()
            self.backend.replace_access_policy.side_effect = BackendError('backend_unavailable', 503)
            self.query.data = 'admin_profile_grants_save:p1'
            await profiles.save_grants_cb(self.query, self.bot, self.backend, self.state)
            await profiles.save_grants_cb(self.query, self.bot, self.backend, self.state)
            calls = self.backend.replace_access_policy.call_args_list
            self.assertEqual(calls[-1].args, calls[-2].args)
            UUID(calls[-1].args[4])
            self.assertEqual(calls[-1].args[3]['rules'], [{'scope': 'all', 'region_id': None, 'protocols': ['awg']}])
            self.backend.replace_access_policy.reset_mock()
            self.data.update(edit_profile_id=None, draft_rules=[])

    async def test_inherited_toggle_is_an_exclusion_and_reenable_does_not_adopt_manual_source(self):
        self.backend.profile_access_policy.return_value = {'revision': 7, 'explicit_grants': [],
            'rules': [{'scope': 'all', 'region_id': None, 'protocols': ['awg']}], 'exclusions': []}
        self.backend.profile_access_policy.side_effect = None
        with patch.object(profiles, 'render', new_callable=AsyncMock):
            await profiles.show_grant_nodes(123, 123, 77, 'p1', self.bot, self.backend, self.state)
            await profiles.change_grant(self.query, 'p1', 'n00', 'awg', False, self.bot, self.backend, self.state)
            self.assertEqual(self.data['draft_exclusions'], [{'node_key': 'n00', 'protocol': 'awg'}])
            self.assertEqual(self.data['draft_grants'], [])
            await profiles.change_grant(self.query, 'p1', 'n00', 'awg', True, self.bot, self.backend, self.state)
        self.assertEqual(self.data['draft_exclusions'], [])
        self.assertEqual(self.data['draft_grants'], [])
        self.assertFalse(policy_ui.changed(self.data))

    async def test_current_bulk_revoke_preserves_rules_and_does_not_exclude_future_nodes(self):
        self.data.update(edit_profile_id='p1', edit_profile_revision=7, draft_grants=[], original_grants=[],
            draft_rules=[{'scope': 'all', 'region_id': None, 'protocols': ['awg']}])
        self.query.data = 'grant_bulk:p1:del:all'
        with patch.object(profiles, 'render', new_callable=AsyncMock):
            await profiles.grant_bulk_cb(self.query, self.bot, self.backend, self.state)
        self.assertEqual(len(self.data['draft_rules']), 1)
        self.assertEqual(len(self.data['draft_exclusions']), 2)
        future = {'key': 'future', 'protocols': ['awg'], 'enabled': True, 'region_id': 'eu'}
        self.assertEqual(policy_ui.effective(self.data, [*self.nodes, future]), {('future', 'awg')})

    async def test_region_paging_uses_arrows_and_stale_tokens_do_not_change_another_page(self):
        self.regions = [{'id': str(i), 'title': f'Region {i:02}'} for i in range(23)]
        with patch.object(profiles, 'render', new_callable=AsyncMock) as draw:
            await profiles.show_future_rules(123, 123, 77, self.bot, self.backend, self.state, 'edit', 'p1')
            stale = draw.call_args.args[2].sections[1].rows[0][0].callback_data
            self.query.data = draw.call_args.args[3][0][-1].callback_data
            await profiles.future_page_cb(self.query, self.bot, self.backend, self.state)
            self.assertEqual([b.text for b in draw.call_args.args[3][0]], ['←', '2/3', '→'])
            self.assertEqual(len(draw.call_args.args[2].sections), 11)
            await self.toggle_future(stale)
            self.assertEqual(self.data['draft_rules'], [])
            await profiles.show_grant_nodes(123, 123, 77, 'p1', self.bot, self.backend, self.state)
            self.assertIsNone(self.data['future_rules_context'])

    async def test_rule_only_creation_without_servers_is_reviewable_and_atomic(self):
        self.backend.admin_nodes.return_value = {'items': []}
        self.backend.create_profile = AsyncMock(return_value={'profile': {'id': 'created'}})
        self.data.update(profile_account_id='owner', draft_profile_name='Alice', profile_command_key=str(uuid4()),
            draft_grants=[], draft_rules=[{'scope': 'all', 'region_id': None, 'protocols': ['awg']}])
        with patch.object(profiles, 'render', new_callable=AsyncMock) as draw:
            await profiles.show_create_review(123, 77, self.bot, self.state)
            self.assertIn('profile_draft_save', [b.callback_data for row in draw.call_args.args[3] for b in row])
            self.assertIn(profiles.tr('en', 'policy.all'), draw.call_args.args[2].plain())
        with patch.object(profiles, '_clear_flow', new_callable=AsyncMock), patch.object(profiles, 'show_admin_profile', new_callable=AsyncMock):
            await profiles.draft_save_cb(self.query, self.bot, self.backend, self.state)
        self.assertEqual(self.backend.create_profile.call_args.kwargs['access_policy']['rules'], self.data['draft_rules'])
        self.backend.replace_access_policy.assert_not_awaited()


class TelegramRegionPolicyConfirmationTests(IsolatedAsyncioTestCase):
    setUp = node_fixture.AdminNodeRichTests.setUp

    async def test_region_move_is_reviewed_before_write_and_stale_confirmation_is_rejected(self):
        self.backend.edit_node = AsyncMock(return_value={**self.node, 'desired_revision': 4})
        await nodes.change_node_draft(123, 'msk1', {'region': 'Asia'}, self.backend, self.state)
        self.backend.request.return_value = {'affected_profiles': 2, 'profiles': []}
        self.query.data = 'node_draft_save:msk1'
        with patch.object(nodes, 'render', new_callable=AsyncMock) as draw:
            await nodes.save_node_draft_cb(self.query, self.bot, self.backend, self.state)
        self.backend.edit_node.assert_not_awaited()
        self.assertIn('2', draw.call_args.args[2].plain())
        confirmation = self.data['node_region_confirmation']
        self.query.data = f"node_region_yes:{confirmation['nonce']}"
        self.query.message.message_id = 78
        await nodes.confirm_node_region_cb(self.query, self.bot, self.backend, self.state)
        self.backend.edit_node.assert_not_awaited()
        self.query.message.message_id = 77
        with patch.object(nodes, 'apply_node', new_callable=AsyncMock):
            await nodes.confirm_node_region_cb(self.query, self.bot, self.backend, self.state)
        self.assertEqual(self.backend.edit_node.call_args.args, (123, 'msk1', 3, {'region': 'Asia', 'confirm_access_change': True}))
        self.assertIsNone(self.data['node_region_confirmation'])

    async def test_editing_the_draft_invalidates_old_region_confirmation(self):
        self.backend.edit_node = AsyncMock()
        await nodes.change_node_draft(123, 'msk1', {'region': 'Asia'}, self.backend, self.state)
        self.backend.request.return_value = {'affected_profiles': 1, 'profiles': []}
        self.query.data = 'node_draft_save:msk1'
        with patch.object(nodes, 'render', new_callable=AsyncMock):
            await nodes.save_node_draft_cb(self.query, self.bot, self.backend, self.state)
        nonce = self.data['node_region_confirmation']['nonce']
        await nodes.change_node_draft(123, 'msk1', {'region': 'Africa'}, self.backend, self.state)
        self.query.data = 'node_region_yes:' + nonce
        await nodes.confirm_node_region_cb(self.query, self.bot, self.backend, self.state)
        self.backend.edit_node.assert_not_awaited()
