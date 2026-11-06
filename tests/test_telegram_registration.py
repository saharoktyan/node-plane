"""Open registration in the bot: settings and the list of accounts without access."""
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.navigation import screen_parents
from telegram_client.routers import registration
from tests import test_telegram_admin_profiles_rich as fixture


class Backend:
    def __init__(self):
        self.calls, self.value = [], {'open': False, 'cleanup_days': None}

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs.get('body')))
        if path.startswith('/api/v1/accounts'):
            return {'items': [{'id': 'a' * 8 + '-0000-4000-8000-000000000001', 'login': 'kim'}]}
        if method == 'PATCH':
            self.value.update(kwargs['body'])
        return dict(self.value)


class RegistrationTests(IsolatedAsyncioTestCase):
    setUp = fixture.ProfileRichTests.setUp

    async def press(self, data, backend):
        self.query.data = data
        with patch.object(registration, 'render', new_callable=AsyncMock) as draw:
            await registration.registration_cb(self.query, self.bot, backend, self.state)
        return draw.call_args.args[2:4]

    def choices(self, screen):
        return [[(b.callback_data, b.style) for b in row] for section in screen.sections for row in section.rows]

    async def test_settings_turn_registration_and_cleanup_on_and_off(self):
        backend = Backend()
        screen, rows = await self.press('reg:v', backend)
        self.assertEqual(self.choices(screen)[0], [('reg:open:on', None), ('reg:open:off', 'primary')])
        self.assertEqual([c.label for c in screen_parents(screen, rows, 'en', self.data)][-1], 'System settings')
        screen, _ = await self.press('reg:open:on', backend)
        self.assertEqual(backend.calls[-2], ('PATCH', '/api/v1/system/registration', {'open': True}))
        self.assertEqual(self.choices(screen)[0][0], ('reg:open:on', 'primary'))
        screen, _ = await self.press('reg:cleanup:30', backend)
        self.assertEqual(backend.calls[-2][2], {'cleanup_days': 30})
        self.assertIn(('reg:cleanup:30', 'primary'), self.choices(screen)[1])
        await self.press('reg:cleanup:off', backend)
        self.assertEqual(backend.calls[-2][2], {'cleanup_days': None})

    async def test_accounts_without_access(self):
        backend = Backend()
        screen, rows = await self.press('reg:without', backend)
        self.assertEqual(backend.calls[-1][1], '/api/v1/accounts?limit=100&access=without')
        self.assertEqual(rows[0][0].text, 'kim')
        self.assertEqual([c.label for c in screen_parents(screen, rows, 'en', self.data)][-1], 'Accounts')
