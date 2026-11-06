"""The web cabinet screen in the bot: where it is served from, set up from Services or Settings."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.backend import BackendError
from telegram_client.navigation import parent_path
from telegram_client.routers import admin_cabinet as cabinet
from tests import test_telegram_admin_profiles_rich as fixture

LOCAL = {'mode': 'local', 'node_key': None, 'listen': '0.0.0.0:8090', 'public_url': 'https://panel.example.com',
         'url': 'https://panel.example.com', 'status': 'active', 'applied_version': '0.4.0', 'version': '0.4.0',
         'error_code': None}


class Backend:
    def __init__(self, cabinet=None, relay='n1', available=True):
        self.cabinet, self.relay, self.available, self.calls = cabinet, relay, available, []

    async def request(self, method, path, **kwargs):
        self.calls.append((method, kwargs.get('body')))
        if method == 'PUT':
            body = kwargs['body']
            if body['mode'] == 'local' and body['listen'].endswith(':81'):
                raise BackendError('cabinet_listen_invalid', 422)
            if self.cabinet and self.cabinet['mode'] != body['mode']:
                raise BackendError('cabinet_mode_change', 409)
            self.cabinet = {**LOCAL, 'listen': None, 'public_url': None, **body, 'status': 'applying',
                            'url': body.get('public_url') or 'https://cab.example.com'}
        elif method == 'DELETE':
            self.cabinet = {**self.cabinet, 'status': 'removing', 'url': None}
        return {'cabinet': self.cabinet, 'available': self.available, 'relay_node_key': self.relay}


class CabinetTests(IsolatedAsyncioTestCase):
    setUp = fixture.ProfileRichTests.setUp

    async def press(self, data, backend):
        self.query.data = data
        with patch.object(cabinet, 'render', new_callable=AsyncMock) as draw:
            await cabinet.cabinet_cb(self.query, self.bot, backend, self.state)
        return draw.call_args.args[2:4]

    async def send(self, text, backend):
        message = SimpleNamespace(chat=SimpleNamespace(id=123, type='private'), from_user=SimpleNamespace(id=123),
                                  text=text, delete=AsyncMock())
        with patch.object(cabinet, 'render', new_callable=AsyncMock) as draw:
            await cabinet.cabinet_value(message, self.bot, backend, self.state)
        return draw.call_args.args[2:4]

    def buttons(self, rows):
        return [button.callback_data for row in rows for button in row]

    async def test_serve_on_the_relay(self):
        backend = Backend()
        screen, rows = await self.press('cab:v:v', backend)
        self.assertIn('not served yet', screen.plain())
        self.assertEqual(rows[-1][0].callback_data, 'services')
        screen, rows = await self.press('cab:r:v', backend)
        self.assertEqual(backend.calls[-2], ('PUT', {'mode': 'relay'}))
        self.assertIn("the relay's node", screen.plain())
        self.assertIn('Status: Applying', screen.plain())
        self.assertNotIn('cab:e:v', self.buttons(rows))

    async def test_without_relay_or_build(self):
        screen, rows = await self.press('cab:v:s', Backend(relay=None))
        self.assertIn('set up the relay first', screen.plain())
        self.assertIn('relay:v:s', self.buttons(rows))
        self.assertNotIn('cab:r:s', self.buttons(rows))
        self.assertIn('cab:e:s', self.buttons(rows))
        screen, rows = await self.press('cab:v:s', Backend(available=False))
        self.assertIn('no web cabinet build', screen.plain())
        self.assertEqual(self.buttons(rows), ['cab:v:s', 'admin_settings'])

    async def test_serve_from_the_controller(self):
        backend = Backend()
        await self.press('cab:e:s', backend)
        screen, _ = await self.press('cab:save:s', backend)
        self.assertIn('public address first', screen.plain())
        await self.press('cab:fl:s', backend)
        screen, _ = await self.send('0.0.0.0:8080', backend)
        self.assertIn("8080 is the API's", screen.plain())
        await self.send('127.0.0.1:81', backend)
        await self.press('cab:fu:s', backend)
        screen, _ = await self.send('panel.example.com', backend)
        self.assertIn('https://', screen.plain())
        await self.send('https://panel.example.com', backend)
        screen, _ = await self.press('cab:save:s', backend)
        self.assertIn('listen address is not valid', screen.plain())
        await self.press('cab:fl:s', backend)
        await self.send('0.0.0.0:8090', backend)
        screen, rows = await self.press('cab:save:s', backend)
        self.assertEqual(backend.calls[-2], ('PUT', {'mode': 'local', 'listen': '0.0.0.0:8090',
                                                     'public_url': 'https://panel.example.com'}))
        self.assertIn('the controller, on 0.0.0.0:8090', screen.plain())
        self.assertIn('cab:e:s', self.buttons(rows))
        self.assertIsNone(self.data['cabinet_draft'])

    async def test_change_keeps_the_values_and_removal_asks_first(self):
        backend = Backend(LOCAL)
        screen, rows = await self.press('cab:e:v', backend)
        self.assertIn('Listen: 0.0.0.0:8090', [b.text for row in rows for b in row])
        screen, rows = await self.press('cab:rm:v', backend)
        self.assertIn('cab:rmy:v', self.buttons(rows))
        self.assertFalse([call for call in backend.calls if call[0] == 'DELETE'])
        screen, rows = await self.press('cab:rmy:v', backend)
        self.assertIn('Status: Removing', screen.plain())
        self.assertNotIn('cab:rm:v', self.buttons(rows))

    def test_breadcrumbs(self):
        self.assertEqual([c.label for c in parent_path('cab:e:s', 'en', self.data)],
                         ['Admin', 'System settings', 'Web cabinet', 'Cabinet on the controller'])
        self.assertEqual(parent_path('cab:v:v', 'en', self.data)[-1].callback, 'cab:v:v')
