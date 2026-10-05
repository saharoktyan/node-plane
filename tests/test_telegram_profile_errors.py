"""Profile read failures must leave a usable, localized screen."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.backend import BackendError
from telegram_client.routers import admin_profiles
from tests import test_telegram_client as fixture


class ProfileErrorTests(IsolatedAsyncioTestCase):
    setUp = fixture.TelegramFlowTests.setUp

    async def test_summary_failure_renders_error_and_back_in_same_message(self):
        async def request(method, path, **kwargs):
            if path.endswith('/summary'):
                raise BackendError('internal_error', 500)
            return {'display_name': 'Admin'}

        backend = SimpleNamespace(request=request, profile_grants=AsyncMock(return_value={'items': []}),
                                  profile_operation=AsyncMock(return_value=None))
        for locale in ('en', 'ru'):
            self.state_data['locale'] = locale
            with patch.object(admin_profiles, 'render', new_callable=AsyncMock) as draw:
                await admin_profiles.show_admin_profile(123, 123, 77, 'profile',
                    self.bot, backend, self.state)
            screen, rows = draw.call_args.args[2:4]
            self.assertIn(admin_profiles.tr(locale, 'profile.admin.error_loading'), screen.lines)
            self.assertNotIn('internal_error', screen.plain())
            self.assertEqual(rows[0][0].callback_data, admin_profiles.AdminProfilesCallback().pack())
            self.assertEqual(draw.call_args.args[-1], 77)
