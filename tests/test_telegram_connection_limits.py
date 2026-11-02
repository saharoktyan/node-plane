"""Bot screens for concurrent VLESS source limits."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.backend import BackendError
from telegram_client.routers import admin_connection_limits as limits
from telegram_client.routers import admin_profiles as profiles
from tests.test_telegram_admin_profiles_rich import ProfileRichTests

DEFAULTS = {'connections.mode': 'off', 'connections.max_sources': 1,
            'connections.window_seconds': 120, 'connections.cooldown_seconds': 300}


class Policies:
    """An in-memory policy API: installation values and one profile's overrides."""

    def __init__(self):
        self.values = {'installation': {}, 'profile': {}}
        self.revisions = {'installation': {}, 'profile': {}}
        self.patches = []

    def listing(self, scope):
        keys = []
        for key, default in DEFAULTS.items():
            installation = self.values['installation'].get(key, default)
            value = self.values[scope].get(key)
            effective = installation if scope == 'installation' else value if value is not None else installation
            keys.append({'key': key, 'value': value, 'default': default, 'effective': effective,
                         'revision': self.revisions[scope].get(key, 0)})
        return {'keys': keys}

    async def request(self, method, path, **kwargs):
        scope = 'installation' if path.endswith('/installation') else 'profile'
        if method == 'PATCH':
            change = kwargs['body']['changes'][0]
            self.patches.append((scope, change['key'], change['value'], change['revision']))
            assert kwargs['command']
            if change['value'] is None:
                self.values[scope].pop(change['key'], None)
            else:
                self.values[scope][change['key']] = change['value']
            self.revisions[scope][change['key']] = change['revision'] + 1
        return self.listing(scope)


class ConnectionLimitScreenTests(IsolatedAsyncioTestCase):
    setUp = ProfileRichTests.setUp

    async def press(self, handler, data, backend):
        self.query.data = data
        with patch.object(limits, 'render', new_callable=AsyncMock) as draw:
            await handler(self.query, self.bot, backend, self.state)
        return draw.call_args.args[2:4]

    @staticmethod
    def selected(screen):
        return [b.callback_data for section in screen.sections for row in section.rows for b in row
                if b.style == 'primary']

    async def test_installation_choices_store_the_default_as_no_value(self):
        policies = Policies()
        backend = SimpleNamespace(request=AsyncMock(side_effect=policies.request))
        screen, rows = await self.press(limits.installation_cb, 'connlimit', backend)
        self.assertEqual(self.selected(screen), ['connlimit:mode:off', 'connlimit:max:1'])
        self.assertEqual(rows[-1][0].callback_data, limits.AdminSettingsCallback().pack())
        self.assertIn('120', screen.lines[1])
        screen, _ = await self.press(limits.installation_cb, 'connlimit:mode:enforce', backend)
        self.assertEqual(self.selected(screen), ['connlimit:mode:enforce', 'connlimit:max:1'])
        await self.press(limits.installation_cb, 'connlimit:max:3', backend)
        # Invalid and unchanged choices write nothing.
        for data in ('connlimit:max:4', 'connlimit:mode:block', 'connlimit:max:3', 'connlimit:max:inherit'):
            await self.press(limits.installation_cb, data, backend)
        await self.press(limits.installation_cb, 'connlimit:mode:off', backend)
        self.assertEqual(policies.patches, [('installation', 'connections.mode', 'enforce', 0),
                                            ('installation', 'connections.max_sources', 3, 0),
                                            ('installation', 'connections.max_sources', None, 1),
                                            ('installation', 'connections.mode', None, 1)])

    async def test_profile_overrides_or_inherits_and_shows_in_the_editor(self):
        policies = Policies()
        policies.values['installation'] = {'connections.mode': 'observe', 'connections.max_sources': 2}

        async def request(method, path, **kwargs):
            return await policies.request(method, path, **kwargs) if 'vpn-policies' in path else self.profile
        self.backend.request = AsyncMock(side_effect=request)
        with patch.object(profiles, 'render', new_callable=AsyncMock) as draw:
            await profiles.show_profile_edit_menu(123, 123, 77, 'p1', self.bot, self.backend, self.state)
        screen, rows = draw.call_args.args[2:4]
        section = next(item for item in screen.sections if item.title == 'Simultaneous networks')
        self.assertEqual(section.lines, ('As installation (Observe, up to 2 per device)',))
        self.assertIn('pconnlimit:p1', [b.callback_data for row in screen.fallback_rows(rows) for b in row])
        screen, rows = await self.press(limits.profile_cb, 'pconnlimit:p1', self.backend)
        self.assertEqual(self.selected(screen), ['pconnlimit:p1:mode:inherit', 'pconnlimit:p1:max:inherit'])
        self.assertEqual(screen.sections[0].rows[0][0].text, 'As installation (Observe)')
        self.assertEqual(rows[-1][0].callback_data, 'admin_profile_edit:p1')
        screen, _ = await self.press(limits.profile_cb, 'pconnlimit:p1:mode:off', self.backend)
        self.assertEqual(self.selected(screen), ['pconnlimit:p1:mode:off', 'pconnlimit:p1:max:inherit'])
        await self.press(limits.profile_cb, 'pconnlimit:p1:mode:inherit', self.backend)
        self.assertEqual(policies.patches, [('profile', 'connections.mode', 'off', 0),
                                            ('profile', 'connections.mode', None, 1)])
        self.assertEqual(limits.profile_section('en', 'p1', policies.listing('profile'))[0].lines,
                         ('As installation (Observe, up to 2 per device)',))
        policies.values['profile'] = {'connections.max_sources': 5}
        self.assertEqual(limits.profile_section('en', 'p1', policies.listing('profile'))[0].lines,
                         ('Observe, up to 5 per device',))

    async def test_backend_errors_replace_the_choices(self):
        backend = SimpleNamespace(request=AsyncMock(side_effect=BackendError('backend_unavailable', 503)))
        screen, _ = await self.press(limits.installation_cb, 'connlimit:mode:enforce', backend)
        self.assertEqual(screen.sections, ())
        self.assertEqual(len(screen.lines), 1)
        screen, _ = await self.press(limits.profile_cb, 'pconnlimit:p1', backend)
        self.assertEqual(screen.sections, ())
