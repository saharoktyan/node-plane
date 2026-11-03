"""Bot screens for traffic quotas."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from telegram_client.notifications import notification_content
from telegram_client.routers import admin_quotas as quotas
from tests import test_telegram_admin_profiles_rich as profile_fixture

GIB = 1024 ** 3
DEFAULTS = {'quota.bytes': None, 'quota.reset': 'calendar_month', 'quota.interval_days': 30,
            'quota.on_exhausted': 'freeze', 'quota.timezone': 'UTC'}


class Policies:
    """An in-memory policy and quota API for one profile."""

    def __init__(self):
        self.values = {'installation': {}, 'profile': {}}
        self.patches, self.resets = [], []
        self.quota = {'limit_bytes': None, 'period': None}

    def listing(self, scope):
        keys = []
        for key, default in DEFAULTS.items():
            if scope == 'profile' and key == 'quota.timezone':
                continue
            installation = self.values['installation'].get(key, default)
            value = self.values[scope].get(key)
            effective = installation if scope == 'installation' else value if value is not None else installation
            keys.append({'key': key, 'value': value, 'default': default, 'effective': effective, 'revision': 0})
        return {'keys': keys}

    async def request(self, method, path, **kwargs):
        if path.endswith('/quota/reset'):
            self.resets.append(kwargs['command_key'])
            return self.quota
        if path.endswith('/quota'):
            return self.quota
        scope = 'installation' if path.endswith('/installation') else 'profile'
        if method == 'PATCH':
            change = kwargs['body']['changes'][0]
            self.patches.append((scope, change['key'], change['value']))
            if change['value'] is None:
                self.values[scope].pop(change['key'], None)
            else:
                self.values[scope][change['key']] = change['value']
        return self.listing(scope)


class QuotaScreenTests(IsolatedAsyncioTestCase):
    setUp = profile_fixture.ProfileRichTests.setUp

    async def press(self, handler, data, backend):
        self.query.data = data
        with patch.object(quotas, 'render', new_callable=AsyncMock) as draw:
            await handler(self.query, self.bot, backend, self.state)
        return draw.call_args.args[2:4]

    @staticmethod
    def selected(screen):
        return [b.callback_data for section in screen.sections for row in section.rows for b in row
                if b.style == 'primary']

    async def test_installation_choices_and_interval_days_only_for_interval(self):
        policies = Policies()
        backend = SimpleNamespace(request=AsyncMock(side_effect=policies.request))
        screen, _ = await self.press(quotas.installation_cb, 'tquota', backend)
        self.assertEqual(self.selected(screen), ['tquota:limit:none', 'tquota:reset:calendar_month',
                                                 'tquota:exhausted:freeze', 'tquota:tz:UTC'])
        await self.press(quotas.installation_cb, f'tquota:limit:{50 * GIB}', backend)
        screen, _ = await self.press(quotas.installation_cb, 'tquota:reset:interval', backend)
        self.assertIn('tquota:days:30', self.selected(screen))
        await self.press(quotas.installation_cb, 'tquota:days:7', backend)
        await self.press(quotas.installation_cb, 'tquota:tz:Europe/Moscow', backend)
        # Invalid values and the default write nothing new.
        for data in ('tquota:limit:123', 'tquota:tz:Mars/Base', 'tquota:days:30'):
            await self.press(quotas.installation_cb, data, backend)
        await self.press(quotas.installation_cb, 'tquota:limit:none', backend)
        self.assertEqual(policies.patches, [
            ('installation', 'quota.bytes', 50 * GIB), ('installation', 'quota.reset', 'interval'),
            ('installation', 'quota.interval_days', 7), ('installation', 'quota.timezone', 'Europe/Moscow'),
            ('installation', 'quota.interval_days', None), ('installation', 'quota.bytes', None)])

    async def test_profile_override_exemption_and_confirmed_reset(self):
        policies = Policies()
        policies.values['installation']['quota.bytes'] = 100 * GIB
        policies.quota = {'limit_bytes': 100 * GIB, 'period': {'used_bytes': 30 * GIB, 'end': '2026-12-01T00:00:00+00:00',
                                                               'blocked': True}}

        async def request(method, path, **kwargs):
            return await policies.request(method, path, **kwargs) if 'quota' in path or 'polic' in path else self.profile
        self.backend.request = AsyncMock(side_effect=request)
        with patch.object(profile_fixture.profiles, 'render', new_callable=AsyncMock) as draw:
            await profile_fixture.profiles.show_profile_edit_menu(123, 123, 77, 'p1', self.bot, self.backend, self.state)
        section = next(item for item in draw.call_args.args[2].sections if item.title == 'Traffic quota')
        self.assertEqual(section.lines, ('30.0 GiB of 100.0 GiB · until 2026-12-01 · access suspended',))
        screen, rows = await self.press(quotas.profile_cb, 'pquota:p1', self.backend)
        self.assertEqual(screen.sections[0].title, 'Current period')
        self.assertIn('pquota:p1:limit:inherit', self.selected(screen))
        self.assertEqual(rows[-1][0].callback_data, 'admin_profile_edit:p1')
        screen, _ = await self.press(quotas.profile_cb, 'pquota:p1:limit:none', self.backend)
        self.assertIn('pquota:p1:limit:none', self.selected(screen))
        await self.press(quotas.profile_cb, 'pquota:p1:limit:inherit', self.backend)
        self.assertEqual(policies.patches, [('profile', 'quota.bytes', 0), ('profile', 'quota.bytes', None)])
        # The reset asks first; the confirmation carries one command key.
        screen, rows = await self.press(quotas.profile_cb, 'pquota:p1:reset_ask', self.backend)
        self.assertEqual(policies.resets, [])
        confirm = rows[0][0].callback_data
        self.assertTrue(confirm.startswith('pquota:p1:reset_now:'))
        self.assertLessEqual(len(confirm.encode()), 64)
        await self.press(quotas.profile_cb, confirm, self.backend)
        self.assertEqual(policies.resets, [confirm.rsplit(':', 1)[1]])


class QuotaTextTests(TestCase):
    def test_notifications_and_member_card_text(self):
        from telegram_client.routers.user import quota_text
        for locale in ('en', 'ru'):
            for kind, payload in (('quota_warning', {'percent': 80, 'used': '8.0 GiB', 'limit': '10.0 GiB'}),
                                  ('quota_exhausted', {'limit': '10.0 GiB', 'suspended': True}),
                                  ('quota_exhausted', {'limit': '10.0 GiB', 'suspended': False}),
                                  ('quota_reset', {})):
                title, lines = notification_content({'notification_kind': kind, 'service_title': 'VPN',
                                                     'payload': payload}, locale)
                self.assertNotIn('notification.', title + ' '.join(lines))
            self.assertIn('2026-12-01', quota_text({'used_bytes': GIB, 'limit_bytes': 10 * GIB,
                                                    'period_end': '2026-12-01T00:00:00+00:00'}, locale))
        title, lines = notification_content({'notification_kind': 'quota_exhausted', 'service_title': 'VPN',
                                             'payload': {'limit': '10.0 GiB', 'suspended': True}}, 'en')
        self.assertIn('suspended', lines[-1])
