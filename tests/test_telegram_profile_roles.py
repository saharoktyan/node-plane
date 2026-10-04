"""Explicit, revision-bound administrator promotion through a profile."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.backend import BackendError
from telegram_client.routers import admin_profiles as profiles


class ProfileRoleTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.data = {'locale': 'en'}
        async def get_data():
            return dict(self.data)
        async def update_data(**values):
            self.data.update(values)
        self.state = SimpleNamespace(get_data=get_data, update_data=update_data)
        self.query = SimpleNamespace(data='prof_promote:p1', answer=AsyncMock(),
            from_user=SimpleNamespace(id=123),
            message=SimpleNamespace(message_id=77, chat=SimpleNamespace(id=123)))
        self.profile = {'id': 'p1', 'display_name': 'Alice', 'owner_account_id': 'a1'}
        self.account = {'id': 'a1', 'username': 'alice', 'telegram_user_id': None,
                        'role': 'member', 'status': 'approved', 'revision': 7}
        async def request(method, path, **kwargs):
            if method == 'PATCH':
                return {**self.account, 'role': 'admin', 'revision': 8}
            return self.profile if '/profiles/' in path else self.account
        self.backend = SimpleNamespace(request=AsyncMock(side_effect=request))
        self.bot = SimpleNamespace()

    async def open_confirmation(self):
        with patch.object(profiles, 'render', new_callable=AsyncMock) as draw:
            await profiles.profile_promote_cb(self.query, self.bot, self.backend, self.state)
        return draw.call_args

    async def test_warning_names_target_and_requires_explicit_second_click_in_both_languages(self):
        for locale in ('en', 'ru'):
            self.data['locale'] = locale
            call = await self.open_confirmation()
            screen, rows = call.args[2:4]
            self.assertIn('@alice', screen.plain())
            self.assertIn(profiles.tr(locale, 'profile.role.warning'), screen.lines)
            self.assertEqual(rows[0][0].text, profiles.tr(locale, 'profile.role.confirm'))
            self.assertEqual(rows[0][0].style, 'danger')
            self.assertEqual(rows[-1][0].callback_data, 'prof_role:p1')
            self.assertFalse(any(c.args[0] == 'PATCH' for c in self.backend.request.call_args_list))

    async def test_confirmation_patches_only_role_with_captured_revision_and_command_key(self):
        await self.open_confirmation()
        confirmation = self.data['admin_promotion'].copy()
        self.query.data = 'promote_yes:' + confirmation['nonce']
        with patch.object(profiles, 'render', new_callable=AsyncMock):
            await profiles.profile_promote_confirm_cb(self.query, self.bot, self.backend, self.state)
        mutation = next(c for c in self.backend.request.call_args_list if c.args[0] == 'PATCH')
        self.assertEqual(mutation.args, ('PATCH', '/api/v1/accounts/a1'))
        self.assertEqual(mutation.kwargs['body'], {'role': 'admin'})
        self.assertEqual(mutation.kwargs['revision'], 7)
        self.assertEqual(mutation.kwargs['telegram_user_id'], 123)
        self.assertEqual(mutation.kwargs['command_key'], confirmation['command_key'])
        self.assertIsNone(self.data['admin_promotion'])
        self.backend.request.reset_mock()
        await profiles.profile_promote_confirm_cb(self.query, self.bot, self.backend, self.state)
        self.backend.request.assert_not_awaited()

    async def test_forged_or_other_message_confirmation_does_not_mutate(self):
        await self.open_confirmation()
        for callback, message_id in [('promote_yes:forged', 77),
                ('promote_yes:' + self.data['admin_promotion']['nonce'], 99)]:
            self.query.data, self.query.message.message_id = callback, message_id
            self.backend.request.reset_mock()
            await profiles.profile_promote_confirm_cb(self.query, self.bot, self.backend, self.state)
            self.backend.request.assert_not_awaited()
            self.assertTrue(self.query.answer.call_args.kwargs['show_alert'])

    async def test_back_discards_confirmation_and_already_admin_has_no_promote_button(self):
        await self.open_confirmation()
        self.account['role'] = 'admin'
        self.query.data = 'prof_role:p1'
        with patch.object(profiles, 'render', new_callable=AsyncMock) as draw:
            await profiles.profile_role_cb(self.query, self.bot, self.backend, self.state)
        self.assertIsNone(self.data['admin_promotion'])
        self.assertEqual(len(draw.call_args.args[3]), 1)

    async def test_pending_account_cannot_open_promotion(self):
        self.account['status'] = 'pending'
        with patch.object(profiles, 'render', new_callable=AsyncMock) as draw:
            await profiles.profile_promote_cb(self.query, self.bot, self.backend, self.state)
        draw.assert_not_awaited()
        self.assertNotIn('admin_promotion', self.data)
        self.assertTrue(self.query.answer.call_args.kwargs['show_alert'])

    async def test_changed_account_revision_is_not_silently_rebased(self):
        await self.open_confirmation()
        original_request = self.backend.request.side_effect
        async def conflicting_request(method, path, **kwargs):
            if method == 'PATCH':
                raise BackendError('revision_conflict', 412)
            return await original_request(method, path, **kwargs)
        self.account['revision'] = 8
        self.backend.request.side_effect = conflicting_request
        self.query.data = 'promote_yes:' + self.data['admin_promotion']['nonce']
        with patch.object(profiles, 'render', new_callable=AsyncMock) as draw:
            await profiles.profile_promote_confirm_cb(self.query, self.bot, self.backend, self.state)
        draw.assert_not_awaited()
        mutation = next(c for c in self.backend.request.call_args_list if c.args[0] == 'PATCH')
        self.assertEqual(mutation.kwargs['revision'], 7)
        self.assertIsNone(self.data['admin_promotion'])
        self.assertTrue(self.query.answer.call_args.kwargs['show_alert'])
