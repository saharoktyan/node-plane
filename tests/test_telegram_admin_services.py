"""The services screen in the bot: kinds, states and instances, the way to the relay and back."""
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.navigation import parent_path, screen_parents
from telegram_client.routers import admin_relay
from telegram_client.routers import admin_services as services
from tests import test_telegram_admin_profiles_rich as fixture

ITEMS = [
    {'service': 'vpn', 'kind': 'builtin', 'profiles': True, 'instance_types': ['xray', 'awg'],
     'access': 'active', 'state': None, 'instances': 3},
    {'service': 'relay', 'kind': 'system', 'profiles': False, 'instance_types': ['relay'],
     'access': None, 'state': 'active', 'instances': 1},
    {'service': 'telegram_bot', 'kind': 'system', 'profiles': False, 'instance_types': [],
     'access': None, 'state': 'active', 'instances': None},
]


class Backend:
    async def request(self, method, path, **kwargs):
        assert (method, path) == ('GET', '/api/v1/services')
        return {'items': ITEMS}


class ServicesTests(IsolatedAsyncioTestCase):
    setUp = fixture.ProfileRichTests.setUp

    async def test_the_catalog_by_kind(self):
        self.query.data = 'services'
        with patch.object(services, 'render', new_callable=AsyncMock) as draw:
            await services.services_cb(self.query, self.bot, Backend(), self.state)
        screen, rows = draw.call_args.args[2:4]
        text = screen.plain()
        self.assertLess(text.index('System'), text.index('Built-in'))
        for value in ('Relay', 'Telegram bot', 'VPN', '3'):
            self.assertIn(value, text)
        self.assertEqual([row[0].callback_data for row in rows], ['relay:v:v', 'tgb:v:v', 'admin_menu'])
        self.assertEqual([c.label for c in screen_parents(screen, rows, 'en', self.data)], ['Admin'])

    def test_the_relay_opened_from_services_goes_back_there(self):
        self.assertEqual(admin_relay.back_callback('v'), 'services')
        self.assertEqual([c.label for c in parent_path('relay:v:v', 'en', {})][-2:], ['Services', 'Relay'])

    def test_the_entry_is_for_staff_beyond_vpn(self):
        self.assertTrue(services.entry_visible({'settings.manage'}))
        self.assertTrue(services.entry_visible({'services.xray.manage'}))
        self.assertFalse(services.entry_visible({'profiles.manage', 'access_requests.manage'}))
