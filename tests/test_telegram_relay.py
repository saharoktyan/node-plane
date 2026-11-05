"""The relay screen in the bot: three ways in, one screen, Back to where it was opened."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.backend import BackendError
from telegram_client.navigation import parent_path, screen_parents
from telegram_client.routers import admin_relay as relay
from tests import test_telegram_admin_profiles_rich as fixture

RELAY = {'node_key': 'n1', 'instance_id': 'i1', 'domain': 'cab.example.com', 'port': 443, 'tls': 'acme',
         'status': 'active', 'revision': 1, 'url': 'https://cab.example.com',
         'link': {'agent_reachable': True, 'running': True, 'connected': True}}


class Backend:
    def __init__(self, relay=None):
        self.relay, self.calls = relay, []

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs.get('body')))
        if path == relay.RELAY and method == 'PUT':
            if kwargs['body']['node_key'] == 'busy':
                raise BackendError('relay_port_in_use', 409)
            self.relay = {**RELAY, **kwargs['body'], 'status': 'applying'}
        elif path == relay.RELAY and method == 'DELETE':
            self.relay = {**self.relay, 'status': 'removing'}
        elif path.startswith('/api/v1/nodes?'):
            return {'items': [{'key': 'n1', 'title': 'One', 'stage': 'node'},
                              {'key': 'n2', 'title': 'Two', 'stage': 'node'}]}
        elif path.startswith('/api/v1/nodes/'):
            return {'key': path.rsplit('/', 1)[1], 'title': 'Node ' + path.rsplit('/', 1)[1]}
        return {'relay': self.relay}


class RelayTests(IsolatedAsyncioTestCase):
    setUp = fixture.ProfileRichTests.setUp

    async def press(self, data, backend):
        self.query.data = data
        with patch.object(relay, 'render', new_callable=AsyncMock) as draw:
            await relay.relay_cb(self.query, self.bot, backend, self.state)
        return draw.call_args.args[2:4]

    async def send(self, text, backend):
        message = SimpleNamespace(chat=SimpleNamespace(id=123, type='private'), from_user=SimpleNamespace(id=123),
                                  text=text, delete=AsyncMock())
        with patch.object(relay, 'render', new_callable=AsyncMock) as draw:
            await relay.relay_value(message, self.bot, backend, self.state)
        return draw.call_args.args[2:4]

    def buttons(self, screen, rows):
        return [button.callback_data for section in screen.sections for row in section.rows for button in row] + [
            button.callback_data for row in rows for button in row]

    async def test_back_and_breadcrumbs_follow_the_way_in(self):
        self.data['_navigation_nodes'] = {'n1': 'Node One'}
        backend = Backend(RELAY)
        for where, back, trail in (('s', 'admin_settings', ['Admin', 'System settings']),
                                   ('c:n1', 'admin_node:n1', ['Admin', 'Nodes', 'Node One']),
                                   ('n:n1', 'node_settings:n1', ['Admin', 'Nodes', 'Node One', 'Advanced & Runtime'])):
            screen, rows = await self.press('relay:v:' + where, backend)
            self.assertEqual(rows[-1][0].callback_data, back)
            self.assertEqual([crumb.label for crumb in screen_parents(screen, rows, 'en', self.data)], trail)
            screen, rows = await self.press('relay:e:' + where, backend)
            self.assertEqual(rows[-1][0].callback_data, 'relay:v:' + where)
            self.assertEqual([crumb.label for crumb in screen_parents(screen, rows, 'en', self.data)], trail + ['Relay'])
        self.assertEqual([c.label for c in parent_path('relay:e:s', 'en', self.data)],
                         ['Admin', 'System settings', 'Relay', 'Relay setup'])

    async def test_set_up_from_settings_chooses_the_node_and_saves(self):
        backend = Backend()
        screen, rows = await self.press('relay:v:s', backend)
        self.assertIn('No relay yet', screen.plain())
        self.assertIn('relay:e:s', self.buttons(screen, rows))
        screen, rows = await self.press('relay:e:s', backend)
        self.assertIn('relay:p1:s', self.buttons(screen, rows))
        screen, _ = await self.press('relay:save:s', backend)
        self.assertIn('Choose a node', screen.plain())
        await self.press('relay:p1:s', backend)
        await self.press('relay:fd:s', backend)
        screen, _ = await self.send('localhost', backend)
        self.assertIn('Enter a domain', screen.plain())
        await self.send(' Cab.Example.com ', backend)
        await self.press('relay:fo:s', backend)
        screen, _ = await self.send('8443', backend)
        self.assertIn('needs port 443', screen.plain())
        await self.press('relay:t1:s', backend)
        await self.press('relay:fo:s', backend)
        await self.send('8443', backend)
        screen, _ = await self.press('relay:save:s', backend)
        self.assertIn('certificate and key paths', screen.plain())
        await self.press('relay:fr:s', backend)
        await self.send('/etc/ssl/c.pem', backend)
        await self.press('relay:fk:s', backend)
        await self.send('/etc/ssl/c.key', backend)
        screen, rows = await self.press('relay:save:s', backend)
        self.assertEqual([call[::2] for call in backend.calls if call[0] == 'PUT'][-1], ('PUT', {'node_key': 'n2', 'domain': 'cab.example.com', 'port': 8443,
            'tls': 'files', 'certificate_path': '/etc/ssl/c.pem', 'key_path': '/etc/ssl/c.key'}))
        self.assertIn('Status: Applying', screen.plain())
        self.assertIsNone(self.data['relay_draft'])

    async def test_a_node_sets_up_on_itself_and_errors_are_explained(self):
        backend = Backend()
        screen, rows = await self.press('relay:e:n:busy', backend)
        self.assertNotIn('relay:p0:n:busy', self.buttons(screen, rows))
        await self.press('relay:fd:n:busy', backend)
        await self.send('cab.example.com', backend)
        screen, _ = await self.press('relay:save:n:busy', backend)
        self.assertIn('already used on this node', screen.plain())

    async def test_removal_asks_first(self):
        backend = Backend(RELAY)
        screen, rows = await self.press('relay:rm:c:n1', backend)
        self.assertIn('relay:rmy:c:n1', self.buttons(screen, rows))
        self.assertFalse([call for call in backend.calls if call[0] == 'DELETE'])
        screen, rows = await self.press('relay:rmy:c:n1', backend)
        self.assertIn('Status: Removing', screen.plain())
        self.assertNotIn('relay:rm:c:n1', self.buttons(screen, rows))

    def test_node_entries(self):
        node = {'key': 'n1', 'service_instances': [{'service': 'awg'}]}
        self.assertEqual(relay.card_rows('en', node), ())
        self.assertEqual(relay.settings_section('en', node, False), ())
        self.assertEqual(relay.settings_section('en', node, True)[0].rows[0][0].text, 'Set up relay here')
        node['service_instances'].append({'service': 'relay'})
        self.assertEqual(relay.card_rows('en', node)[0][0].callback_data, 'relay:v:c:n1')
        self.assertEqual(relay.settings_section('en', node, True)[0].rows[0][0].callback_data, 'relay:v:n:n1')
