"""Device and node overrides of connection and speed limits in the bot."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.routers import admin_policy_overrides as overrides
from telegram_client.routers import user, user_devices
from tests import test_telegram_admin_profiles_rich as fixture

KEYS = {'connections.mode': 'off', 'connections.max_sources': 1, 'speed.up_kbit': None, 'speed.down_kbit': None}


class Store:
    """Installation, profile and device values with node values on the installation."""

    def __init__(self):
        self.installation = {'speed.down_kbit': 20000}
        self.profile = {'connections.mode': 'enforce'}
        self.device, self.nodes, self.patches = {}, {}, []

    def listing(self, own, parent):
        return [{'key': key, 'value': own.get(key), 'default': default, 'revision': 0,
                 'effective': own.get(key, parent.get(key, default))} for key, default in KEYS.items()]

    async def request(self, method, path, **kwargs):
        if path == '/api/v1/me':
            return {'role': 'admin', 'status': 'approved'}
        if method == 'PATCH':
            change = kwargs['body']['changes'][0]
            self.patches.append((path.rsplit('/', 2)[-2] if 'devices' in path else change.get('node_key'),
                                 change['key'], change['value']))
            target = self.device if 'devices' in path else self.nodes.setdefault(change['node_key'], {})
            if change['value'] is None:
                target.pop(change['key'], None)
            else:
                target[change['key']] = change['value']
        if '/devices/' in path:
            return {'keys': self.listing(self.device, {**self.installation, **self.profile}), 'values': []}
        if path.endswith('/vpn-policies'):
            return {'keys': self.listing(self.profile, self.installation), 'values': []}
        return {'keys': self.listing(self.installation, {}),
                'values': [{'key': key, 'node_key': node, 'protocol': None, 'value': value, 'revision': 0}
                           for node, values in self.nodes.items() for key, value in values.items()]}


def primary(screen):
    return [b.text for section in screen.sections for row in section.rows for b in row if b.style == 'primary']


class OverrideScreenTests(IsolatedAsyncioTestCase):
    setUp = fixture.ProfileRichTests.setUp

    async def test_node_values_override_the_installation_and_inherit_removes_them(self):
        store = Store()
        backend = SimpleNamespace(request=AsyncMock(side_effect=store.request))
        node = {'key': 'n1', 'protocols': ['xray']}
        self.assertEqual(overrides.node_section('en', node, await store.request('GET', overrides.INSTALLATION))[0].lines,
                         ('As installation',))

        async def press(data):
            self.query.data = data
            with patch.object(overrides, 'render', new_callable=AsyncMock) as draw:
                await overrides.node_cb(self.query, self.bot, backend, self.state)
            return draw.call_args.args[2]
        screen = await press('nodepol:n1')
        self.assertEqual(screen.sections[3].rows[0][0].text, 'As installation (20 Mbit/s)')
        await press('nodepol:n1:up:5000')
        await press('nodepol:n1:mode:observe')
        screen = await press('nodepol:n1:max:7')  # not offered: nothing written
        self.assertIn('5 Mbit/s', primary(screen))
        self.assertEqual(overrides.node_section('en', node, await store.request('GET', overrides.INSTALLATION))[0].lines,
                         ('Mode: Observe; Upload per device: 5 Mbit/s',))
        await press('nodepol:n1:up:inherit')
        self.assertEqual(store.patches, [('n1', 'speed.up_kbit', 5000), ('n1', 'connections.mode', 'observe'),
                                         ('n1', 'speed.up_kbit', None)])

    async def test_admins_open_device_limits_from_the_device_card(self):
        store = Store()
        items = [{'id': 'd1', 'display_name': 'Phone', 'status': 'active', 'revision': 1}]

        async def request(method, path, **kwargs):
            return {'items': items} if path.endswith('/devices') else await store.request(method, path, **kwargs)
        backend = SimpleNamespace(request=AsyncMock(side_effect=request))
        with patch.object(user_devices, 'render', new_callable=AsyncMock) as draw:
            await user_devices.show_card(123, 123, 77, 'p1', 'd1', self.bot, backend, self.state)
        rows = draw.call_args.args[3]
        entry = next(b for row in rows for b in row if b.text == 'Device limits')
        action = user.actions[entry.callback_data.removeprefix('u:')]
        self.assertEqual((action.name, action.args), ('device_policy', ('p1', 'd1')))
        with patch.object(overrides, 'render', new_callable=AsyncMock) as draw:
            await overrides.show_device(123, 123, 77, 'p1', 'd1', 'down', '0', self.bot, backend, self.state)
        screen = draw.call_args.args[2]
        # Inherited from the profile: enforce; own value: no download limit.
        self.assertEqual(primary(screen), ['As profile (Enforce)', 'As profile (1)', 'As profile (No limit)',
                                           'No limit'])
        self.assertEqual(store.patches, [('d1', 'speed.down_kbit', 0)])
        # Members do not see the entry.

        async def member(method, path, **kwargs):
            return {'role': 'member', 'status': 'approved'} if path == '/api/v1/me' else await request(method, path, **kwargs)
        backend.request = AsyncMock(side_effect=member)
        with patch.object(user_devices, 'render', new_callable=AsyncMock) as draw:
            await user_devices.show_card(123, 123, 77, 'p1', 'd1', self.bot, backend, self.state)
        self.assertNotIn('Device limits', [b.text for row in draw.call_args.args[3] for b in row])
