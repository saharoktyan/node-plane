"""Tariffs in the bot: the catalog, its limits and a profile's tariff."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.routers import admin_tariffs as tariffs
from telegram_client.routers import localized_editor
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
        if path == '/api/v1/system/languages':
            return {'languages': ['en', 'ru']}
        if path.startswith('/api/v1/regions'):
            return {'items': [{'id': 'r1', 'title': 'Europe'}], 'next_cursor': None}
        if path == '/api/v1/tariffs':
            if method == 'POST':
                titles = kwargs['body']['titles']
                self.tariff.update(title=titles['texts'][titles['source']], titles=titles)
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
        with patch.object(localized_editor, 'render', new_callable=AsyncMock):
            await tariffs.texts_cb(self.query, self.bot, backend, self.state)
            await localized_editor.text_message(message, self.bot, backend, self.state)
        self.query.data = 'lt:ok'
        with patch.object(tariffs, 'render', new_callable=AsyncMock) as draw:
            await localized_editor.editor_cb(self.query, self.bot, backend, self.state)
        self.assertEqual(store.calls[0][:3], ('POST', '/api/v1/tariffs',
                                             {'titles': {'source': 'en', 'texts': {'en': 'Basic'}, 'fallback': {}}}))
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


class InheritTextTests(IsolatedAsyncioTestCase):
    def test_a_tariff_value_is_named_as_the_inherited_choice(self):
        label = str
        self.assertEqual(tariffs.inherit_text('en', {'tariff': 2}, 5, label, 'devlimit.inherit'), 'As tariff (2)')
        self.assertEqual(tariffs.inherit_text('en', {'tariff': None}, 5, label, 'devlimit.inherit'),
                         'As installation (5)')


class RenewalScreenTests(IsolatedAsyncioTestCase):
    setUp = fixture.ProfileRichTests.setUp

    async def test_member_asks_once_and_administrators_are_told_about_new_requests(self):
        from telegram_client.routers import admin_renewals, user
        summary = {'profile_id': 'p1', 'display_name': 'Alice', 'frozen': False, 'expired': False,
                   'expires_at': '2026-12-01T00:00:00+00:00', 'nodes': [], 'renewal': None}
        backend = SimpleNamespace(member_profile_summary=AsyncMock(return_value=summary),
                                  request=AsyncMock(return_value={'id': 'r1', 'created': True}))
        with patch.object(user, 'render', new_callable=AsyncMock) as draw:
            await user.show_account_profile(123, 123, 77, 'p1', self.bot, backend, self.state)
        entry = next(b for row in draw.call_args.args[3] for b in row if b.text == 'Extend access')
        action = user.actions[entry.callback_data.removeprefix('u:')]
        self.assertEqual((action.name, action.args), ('renewal_request', ('p1', 'home')))
        summary['renewal'] = {'status': 'pending'}
        with patch.object(user, 'render', new_callable=AsyncMock) as draw:
            await user.show_account_profile(123, 123, 77, 'p1', self.bot, backend, self.state)
        self.assertNotIn('Extend access', [b.text for row in draw.call_args.args[3] for b in row])
        self.assertIn(('Renewal', 'Requested, waiting for an administrator'),
                      draw.call_args.args[2].sections[0].tables[0].rows)
        # Access without a term offers no renewal.
        summary.update(expires_at=None, renewal=None)
        with patch.object(user, 'render', new_callable=AsyncMock) as draw:
            await user.show_account_profile(123, 123, 77, 'p1', self.bot, backend, self.state)
        self.assertNotIn('Extend access', [b.text for row in draw.call_args.args[3] for b in row])
        self.assertEqual([b.callback_data for row in admin_renewals.decision_rows('en', {'id': 'r1',
            'tariff': {'duration_days': 30}}) for b in row], ['rnw_ok:r1:30', 'rnw_no:r1'])

    async def test_decisions_use_the_seen_revision_and_offered_days(self):
        from telegram_client.routers import admin_renewals
        item = {'id': 'r1', 'status': 'pending', 'profile_revision': 9, 'tariff': None,
                'display_name': 'Alice', 'profile_expires_at': None, 'created_at': '2026-11-03T10:00:00'}
        calls = []

        async def request(method, path, **kwargs):
            calls.append((method, path, kwargs.get('body'), kwargs.get('revision')))
            if path.endswith('/approve'):
                item.update(status='approved', expires_at='2027-01-01T00:00:00+00:00')
            return item
        backend = SimpleNamespace(request=AsyncMock(side_effect=request))
        self.query.data = 'rnw_ok:r1:45'  # not offered
        with patch.object(admin_renewals, 'render', new_callable=AsyncMock):
            await admin_renewals.decide_cb(self.query, self.bot, backend, self.state)
        self.assertEqual([c[0] for c in calls], ['GET'])
        self.query.data = 'rnw_ok:r1:30'
        with patch.object(admin_renewals, 'render', new_callable=AsyncMock) as draw:
            await admin_renewals.decide_cb(self.query, self.bot, backend, self.state)
        self.assertIn(('POST', '/api/v1/renewal-requests/r1/approve', {'days': 30}, 9), calls)
        self.assertIn('Access extended until 2027-01-01.', draw.call_args.args[2].lines)
        # A decided request is not decided again.
        self.query.data = 'rnw_no:r1'
        calls.clear()
        with patch.object(admin_renewals, 'render', new_callable=AsyncMock):
            await admin_renewals.decide_cb(self.query, self.bot, backend, self.state)
        self.assertEqual([c[0] for c in calls], ['GET'])
