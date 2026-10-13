from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.routers import user
from telegram_client.screens import Screen, Breadcrumb
from tests import test_telegram_client as fixture


class ProfileDetailsTests(IsolatedAsyncioTestCase):
    setUp = fixture.TelegramFlowTests.setUp

    async def test_details_callback_renders_valid_navigation_with_unknown_traffic(self):
        summary = {'display_name': 'Alice', 'frozen': False, 'expired': False,
            'nodes': [{'key': 'n1', 'title': 'Node', 'region': 'Europe', 'protocols': ['awg']}],
            'traffic': {'status': 'unknown', 'month': '2026-10',
                'items': [{'protocol': 'awg', 'uplink_bytes': None, 'downlink_bytes': None}],
                'nodes': [{'node_key': 'n1', 'protocol': 'awg', 'status': 'unknown',
                           'uplink_bytes': None, 'downlink_bytes': None}]}}
        backend = SimpleNamespace(member_profile_summary=AsyncMock(return_value=summary))
        self.query.from_user.username = 'alice'
        for locale in ('ru', 'en'):
            self.state_data['locale'] = locale
            self.query.data = user.button(123, 'Details', 'account_details', 'p1', 'home').callback_data
            with patch.object(user, 'render', new_callable=AsyncMock) as draw:
                await user.user_action_cb(self.query, self.bot, backend, self.state)
            screen, rows = draw.call_args.args[2:4]
            self.assertEqual(screen.title, user.tr(locale, 'account.details'))
            self.assertTrue(all(p.callback for p in screen.breadcrumbs))
            self.assertEqual(len(screen.breadcrumbs), 2)
            self.assertTrue(all(b.callback_data or b.url for row in screen.fallback_rows(rows) for b in row))
            self.assertIn(user.tr(locale, 'account.rich.waiting'), screen.plain())
            screen.rich(rows)
            back = user.actions[rows[-1][0].callback_data[2:]]
            self.assertEqual(back.name, 'account_profile')
            user.profile_statistics(summary, locale)
            user.profile_node_traffic(summary, summary['nodes'][0], locale)

    def test_static_breadcrumbs_do_not_create_buttons_without_actions(self):
        screen = Screen('Details', breadcrumbs=(Breadcrumb('Profile', 'profile:p1'), Breadcrumb('Static', None)))
        self.assertEqual([b.callback_data for row in screen.fallback_rows([]) for b in row], ['profile:p1'])
        for item in screen.rich().blocks[0].text:
            if getattr(item, 'type', None) == 'button':
                self.assertTrue(item.button.callback_data or item.button.url)
