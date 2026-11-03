"""Bot screens for speed limits."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.routers import admin_profiles as profiles
from telegram_client.routers import admin_speed_limits as speed
from tests import test_telegram_admin_profiles_rich as profile_fixture
from tests import test_telegram_connection_limits as limits_fixture


class Policies(limits_fixture.Policies):
    """The connection-limit policy double with the speed keys."""

    def listing(self, scope):
        keys = []
        for key in ('speed.up_kbit', 'speed.down_kbit'):
            installation = self.values['installation'].get(key)
            value = self.values[scope].get(key)
            effective = installation if scope == 'installation' else value if value is not None else installation
            keys.append({'key': key, 'value': value, 'default': None, 'effective': effective,
                         'revision': self.revisions[scope].get(key, 0)})
        return {'keys': keys}

    async def request(self, method, path, **kwargs):
        if path == '/api/v1/speed-limits/nodes':
            return {'items': self.delivery}
        return await super().request(method, path, **kwargs)


class SpeedLimitScreenTests(IsolatedAsyncioTestCase):
    setUp = profile_fixture.ProfileRichTests.setUp

    async def press(self, handler, data, backend):
        self.query.data = data
        with patch.object(speed, 'render', new_callable=AsyncMock) as draw:
            await handler(self.query, self.bot, backend, self.state)
        return draw.call_args.args[2:4]

    @staticmethod
    def selected(screen):
        return [b.callback_data for section in screen.sections for row in section.rows for b in row
                if b.style == 'primary']

    async def test_installation_limits_and_no_limit_as_no_value(self):
        policies = Policies()
        backend = SimpleNamespace(request=AsyncMock(side_effect=policies.request))
        screen, rows = await self.press(speed.installation_cb, 'speedlim', backend)
        self.assertEqual(self.selected(screen), ['speedlim:up:none', 'speedlim:down:none'])
        self.assertEqual(rows[-1][0].callback_data, speed.AdminSettingsCallback().pack())
        screen, _ = await self.press(speed.installation_cb, 'speedlim:down:20000', backend)
        self.assertEqual(self.selected(screen), ['speedlim:up:none', 'speedlim:down:20000'])
        self.assertIn('20 Mbit/s', [b.text for s in screen.sections for r in s.rows for b in r if b.style])
        # Invalid and unchanged choices write nothing.
        for data in ('speedlim:down:123', 'speedlim:down:20000', 'speedlim:side:1000', 'speedlim:up:inherit'):
            await self.press(speed.installation_cb, data, backend)
        await self.press(speed.installation_cb, 'speedlim:down:none', backend)
        self.assertEqual(policies.patches, [('installation', 'speed.down_kbit', 20000, 0),
                                            ('installation', 'speed.down_kbit', None, 1)])

    async def test_profile_overrides_exempts_and_shows_in_the_editor(self):
        policies = Policies()
        policies.values['installation'] = {'speed.down_kbit': 10000}

        async def request(method, path, **kwargs):
            return await policies.request(method, path, **kwargs) if 'vpn-policies' in path else self.profile
        self.backend.request = AsyncMock(side_effect=request)
        with patch.object(profiles, 'render', new_callable=AsyncMock) as draw:
            await profiles.show_profile_edit_menu(123, 123, 77, 'p1', self.bot, self.backend, self.state)
        screen, rows = draw.call_args.args[2:4]
        section = next(item for item in screen.sections if item.title == 'Speed limit')
        self.assertEqual(section.lines, ('As installation (upload unlimited, download 10 Mbit/s)',))
        self.assertIn('pspeedlim:p1', [b.callback_data for row in screen.fallback_rows(rows) for b in row])
        screen, rows = await self.press(speed.profile_cb, 'pspeedlim:p1', self.backend)
        self.assertEqual(self.selected(screen), ['pspeedlim:p1:up:inherit', 'pspeedlim:p1:down:inherit'])
        self.assertEqual(rows[-1][0].callback_data, 'admin_profile_edit:p1')
        await self.press(speed.profile_cb, 'pspeedlim:p1:down:none', self.backend)
        screen, _ = await self.press(speed.profile_cb, 'pspeedlim:p1:up:5000', self.backend)
        self.assertEqual(self.selected(screen), ['pspeedlim:p1:up:5000', 'pspeedlim:p1:down:none'])
        self.assertEqual(speed.profile_section('en', 'p1', policies.listing('profile'))[0].lines,
                         ('upload 5 Mbit/s, download unlimited',))
        await self.press(speed.profile_cb, 'pspeedlim:p1:down:inherit', self.backend)
        self.assertEqual(policies.patches, [('profile', 'speed.down_kbit', 0, 0), ('profile', 'speed.up_kbit', 5000, 0),
                                            ('profile', 'speed.down_kbit', None, 1)])

    async def test_nodes_that_cannot_shape_are_named(self):
        policies = Policies()
        policies.delivery = [
            {'node_key': 'a', 'title': 'Riga', 'flag': '', 'status': 'current',
             'protocols': [{'protocol': 'xray', 'state': 'unsupported', 'detail': 'nft is not installed'},
                           {'protocol': 'awg', 'state': 'applied', 'detail': ''}]},
            {'node_key': 'b', 'title': 'Berlin', 'flag': '', 'status': 'outdated', 'protocols': []}]
        backend = SimpleNamespace(request=AsyncMock(side_effect=policies.request))
        for locale in ('en', 'ru'):
            await self.state.update_data(locale=locale)
            screen, _ = await self.press(speed.installation_cb, 'speedlim', backend)
            self.assertFalse(any('speed.' in line or 'connlimit.' in line for line in screen.sections[-1].lines))
        await self.state.update_data(locale='en')
        screen, _ = await self.press(speed.installation_cb, 'speedlim', backend)
        self.assertEqual(screen.sections[-1].lines, (
            'Up to date: 1 of 2.', 'Riga, VLESS: the server cannot limit speed (nft is not installed)',
            'Berlin: update the node agent to enforce limits'))
