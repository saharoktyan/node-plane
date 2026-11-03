"""Tariffs in the bot: the catalog, its limits and a profile's tariff."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.routers import admin_tariffs as tariffs
from tests import test_telegram_admin_profiles_rich as fixture

TARIFF = '11111111-1111-4111-8111-111111111111'


class Store:
    def __init__(self):
        self.tariff = {'id': TARIFF, 'title': 'Basic', 'duration_days': 30, 'access_policy': None,
                       'policies': {}, 'revision': 1, 'profiles': 0}
        self.profile = {'id': 'p1', 'desired_revision': 7, 'owner_account_id': None,
                        'expires_at': '2026-12-01T00:00:00+00:00'}
        self.calls = []

    async def request(self, method, path, **kwargs):
        if method != 'GET':
            self.calls.append((method, path, kwargs.get('body'), kwargs.get('revision'), kwargs.get('command_key')))
        if path.startswith('/api/v1/regions'):
            return {'items': [{'id': 'r1', 'title': 'Europe'}], 'next_cursor': None}
        if path == '/api/v1/tariffs':
            if method == 'POST':
                self.tariff['title'] = kwargs['body']['title']
                return self.tariff
            return {'items': [self.tariff], 'keys': []}
        if path.startswith('/api/v1/tariffs/'):
            if method == 'PATCH':
                self.tariff.update(kwargs['body'])
                self.tariff['revision'] += 1
            return self.tariff
        if path.endswith('/tariff'):
            return {}
        return self.profile


class TariffScreenTests(IsolatedAsyncioTestCase):
    setUp = fixture.ProfileRichTests.setUp

    async def press(self, handler, data, backend):
        self.query.data = data
        with patch.object(tariffs, 'render', new_callable=AsyncMock) as draw:
            await handler(self.query, self.bot, backend, self.state)
        return draw.call_args

    async def test_created_tariff_gets_a_term_nodes_and_limits(self):
        store = Store()
        backend = SimpleNamespace(request=AsyncMock(side_effect=store.request))
        message = SimpleNamespace(chat=SimpleNamespace(id=123, type='private'), from_user=SimpleNamespace(id=123),
                                  text=' Basic ', delete=AsyncMock())
        self.query.data = 'trf_new'
        with patch.object(tariffs, 'render', new_callable=AsyncMock):
            await tariffs.title_prompt_cb(self.query, self.bot, self.state)
        with patch.object(tariffs, 'render', new_callable=AsyncMock) as draw:
            await tariffs.title_message(message, self.bot, backend, self.state)
        self.assertEqual(store.calls[0][:3], ('POST', '/api/v1/tariffs', {'title': 'Basic'}))
        self.assertIn('30 days', draw.call_args.args[2].plain())
        self.assertEqual(self.data['tariff_id'], TARIFF)
        await self.press(tariffs.term_cb, 'trf_term:90', backend)
        self.assertEqual(store.calls[-1][2:4], ({'duration_days': 90}, 1))
        await self.press(tariffs.nodes_cb, 'trf_nodes:0', backend)
        self.assertEqual(store.calls[-1][2]['access_policy']['rules'],
                         [{'scope': 'region', 'region_id': 'r1', 'protocols': ['awg', 'xray']}])
        await self.press(tariffs.limits_cb, 'trf_limits:up:5000', backend)
        await self.press(tariffs.limits_cb, 'trf_limits:dev:2', backend)
        # A value that is not offered writes nothing.
        await self.press(tariffs.limits_cb, 'trf_limits:dev:7', backend)
        self.assertEqual(store.tariff['policies'], {'speed.up_kbit': 5000, 'devices.max': 2})
        await self.press(tariffs.limits_cb, 'trf_limits:up:unset', backend)
        self.assertEqual(store.tariff['policies'], {'devices.max': 2})
        self.assertEqual(store.calls[-1][3], store.tariff['revision'] - 1)

    async def test_profile_tariff_extends_the_term_and_a_second_tap_sends_nothing(self):
        store = Store()
        store.tariff['access_policy'] = {'explicit_grants': [], 'exclusions': [],
                                         'rules': [{'scope': 'all', 'region_id': None, 'protocols': ['awg', 'xray']}]}
        backend = SimpleNamespace(request=AsyncMock(side_effect=store.request))
        call = await self.press(tariffs.profile_pick_cb, 'ptrf:p1', backend)
        self.assertEqual([b.callback_data for row in call.args[3] for b in row][:2], ['ptrf_pick:0', 'ptrf_pick:none'])
        call = await self.press(tariffs.profile_choice_cb, 'ptrf_pick:0', backend)
        # An existing term is extended by default; nodes are kept unless chosen.
        self.assertEqual(self.data['tariff_draft']['expiry'], 'extend')
        self.assertIn('Adds 30 days after the remaining time.', call.args[2].plain())
        await self.press(tariffs.profile_option_cb, 'ptrf_opt:access', backend)
        with patch('telegram_client.routers.admin_profiles.show_profile_edit_menu', new_callable=AsyncMock) as shown:
            await self.press(tariffs.profile_apply_cb, 'ptrf_go', backend)
            await self.press(tariffs.profile_apply_cb, 'ptrf_go', backend)
        shown.assert_awaited()
        method, path, body, revision, key = store.calls[-1]
        self.assertEqual((method, path, body, revision),
                         ('PUT', '/api/v1/profiles/p1/tariff',
                          {'tariff_id': TARIFF, 'expiry': 'extend', 'apply_access': True}, 7))
        self.assertEqual(len(store.calls), 1)
        self.assertIsNotNone(key)

    async def test_unlinking_keeps_term_and_nodes(self):
        store = Store()
        backend = SimpleNamespace(request=AsyncMock(side_effect=store.request))
        await self.press(tariffs.profile_pick_cb, 'ptrf:p1', backend)
        await self.press(tariffs.profile_choice_cb, 'ptrf_pick:none', backend)
        with patch('telegram_client.routers.admin_profiles.show_profile_edit_menu', new_callable=AsyncMock):
            await self.press(tariffs.profile_apply_cb, 'ptrf_go', backend)
        self.assertEqual(store.calls[-1][2], {'tariff_id': None, 'expiry': 'keep', 'apply_access': False})
