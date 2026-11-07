"""The Telegram bot screen in the bot: where the bot runs, moved to a node and back."""
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.backend import BackendError
from telegram_client.navigation import parent_path
from telegram_client.routers import admin_telegram_bot as screen_module
from tests import test_telegram_admin_profiles_rich as fixture

NODE = {'key': 'n1', 'title': 'Frankfurt', 'stage': 'node', 'region': None, 'flag': None}


class Backend:
    def __init__(self, placement=None, relay='n1'):
        self.placement, self.relay, self.calls = placement, relay, []

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs.get('body')))
        if path.startswith('/api/v1/nodes?'):
            return {'items': [NODE, {**NODE, 'key': 'n2', 'stage': 'invitation'}]}
        if path.startswith('/api/v1/nodes/'):
            return NODE
        if method == 'PUT':
            if self.placement and self.placement['node_key'] != kwargs['body']['node_key']:
                raise BackendError('telegram_bot_node_change', 409)
            self.placement = {'node_key': kwargs['body']['node_key'], 'status': 'applying', 'version': '0.5.0',
                              'applied_version': None, 'error_code': None}
        elif method == 'DELETE':
            self.placement = {**self.placement, 'status': 'removing'}
        return {'placement': self.placement, 'telegram_enabled': True, 'relay_node_key': self.relay}


class TelegramBotScreenTests(IsolatedAsyncioTestCase):
    setUp = fixture.ProfileRichTests.setUp

    async def press(self, data, backend):
        self.query.data = data
        with patch.object(screen_module, 'render', new_callable=AsyncMock) as draw:
            await screen_module.telegram_bot_cb(self.query, self.bot, backend, self.state)
        return draw.call_args.args[2:4]

    def buttons(self, rows):
        return [button.callback_data for row in rows for button in row]

    async def test_moved_to_a_node_and_back(self):
        backend = Backend()
        screen, rows = await self.press('tgb:v:v', backend)
        self.assertIn('Runs on: the controller', screen.plain())
        self.assertEqual(rows[-1][0].callback_data, 'services')
        screen, rows = await self.press('tgb:n:v', backend)
        # Only nodes with an agent.
        self.assertEqual(self.buttons(rows), ['tgb:p0:v', 'tgb:v:v'])
        screen, rows = await self.press('tgb:p0:v', backend)
        self.assertEqual(backend.calls[-3][0:3], ('PUT', screen_module.PLACEMENT, {'node_key': 'n1'}))
        self.assertIn('Runs on: Frankfurt', screen.plain())
        self.assertIn('Status: Applying', screen.plain())
        screen, rows = await self.press('tgb:rm:v', backend)
        self.assertIn('tgb:rmy:v', self.buttons(rows))
        self.assertFalse([call for call in backend.calls if call[0] == 'DELETE'])
        screen, rows = await self.press('tgb:rmy:v', backend)
        self.assertIn('Status: Removing', screen.plain())
        self.assertNotIn('tgb:rm:v', self.buttons(rows))

    async def test_without_relay_and_after_a_failure(self):
        screen, rows = await self.press('tgb:v:s', Backend(relay=None))
        self.assertIn('set up the relay first', screen.plain())
        self.assertIn('relay:v:s', self.buttons(rows))
        self.assertNotIn('tgb:n:s', self.buttons(rows))
        backend = Backend({'node_key': 'n1', 'status': 'failed', 'version': '0.5.0', 'applied_version': None,
                           'error_code': 'bot_install_failed'})
        screen, rows = await self.press('tgb:v:s', backend)
        self.assertIn('bot_install_failed', screen.plain())
        await self.press('tgb:again:s', backend)
        self.assertIn(('PUT', screen_module.PLACEMENT, {'node_key': 'n1'}), backend.calls)

    def test_breadcrumbs(self):
        self.assertEqual([c.label for c in parent_path('tgb:n:s', 'en', self.data)],
                         ['Admin', 'System settings', 'Telegram bot', 'Run on a node'])
