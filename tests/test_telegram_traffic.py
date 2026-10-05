"""Localized traffic settings and member summaries on the single-message UI."""

from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.i18n import tr
from telegram_client.routers import admin_settings, user

from tests import test_telegram_client as fixture


class TelegramTrafficTests(IsolatedAsyncioTestCase):
    setUp = fixture.TelegramFlowTests.setUp

    async def test_member_settings_do_not_show_admin_only_traffic_policy(self):
        for locale in ('ru', 'en'):
            self.state_data['locale'] = locale
            for available in (False, True):
                backend = SimpleNamespace(me=AsyncMock(return_value={'traffic_available': available}))
                with patch.object(user, 'render', new_callable=AsyncMock) as draw:
                    await user.show_member_settings(123, 123, 77, self.bot, backend, self.state)
                screen = draw.call_args.args[2]
                self.assertNotIn(tr(locale, 'ui.traffic'), [section.title for section in screen.sections])
                labels = [b.text for row in screen.fallback_rows(draw.call_args.args[3]) for b in row]
                self.assertNotIn(tr(locale, 'ui.give_consent'), labels)
                self.assertNotIn(tr(locale, 'ui.withdraw_consent'), labels)

    async def test_admin_toggle_sets_explicit_value_and_shows_sampling_status(self):
        backend = SimpleNamespace(
            update_traffic_policy=AsyncMock(
                return_value={
                    "enabled": True,
                    "interval_minutes": 5,
                    "last_scan": {
                        "at": "2026-09-30T12:00:00+00:00",
                        "profiles_checked": 3,
                        "unknown": 1,
                    },
                }
            )
        )
        for locale in ("en", "ru"):
            self.state_data["locale"] = locale
            self.query.data = "traffic:on"
            with patch.object(admin_settings, "render", new_callable=AsyncMock) as draw:
                await admin_settings.traffic_settings_cb(
                    self.query, self.bot, backend, self.state
                )
            backend.update_traffic_policy.assert_awaited_with(123, True)
            screen = draw.call_args.args[2]
            self.assertEqual([b.callback_data for b in screen.sections[0].rows[0]], ["traffic:on", "traffic:off"])
            self.assertEqual([b.style for b in screen.sections[0].rows[0]], ['primary', None])
            self.assertIn(
                tr(locale, "traffic.scan", at="2026-09-30 12:00", checked=3, unknown=1),
                screen.sections[1].lines,
            )

    async def test_member_stats_keep_last_totals_and_mark_unknown_in_both_locales(self):
        summary = {
            "display_name": "Profile",
            "created_at": None,
            "node_count": 1,
            "protocol_count": 1,
            "xray_count": 1,
            "awg_count": 0,
            "issued_count": 1,
            "last_issued_at": None,
            "traffic": {
                "status": "unknown",
                "month": "2026-09",
                "items": [
                    {
                        "protocol": "xray",
                        "uplink_bytes": 1024,
                        "downlink_bytes": 2048,
                        "tracked_since": "2026-09-30T10:00:00+00:00",
                        "last_sample_at": "2026-09-30T12:00:00+00:00",
                    }
                ],
            },
        }
        backend = SimpleNamespace(
            member_profile_summary=AsyncMock(return_value=summary)
        )
        for locale in ("ru", "en"):
            self.state_data["locale"] = locale
            with patch.object(user, "render", new_callable=AsyncMock) as draw:
                await user.show_account_stats(
                    123, 123, 77, "p1", self.bot, backend, self.state
                )
            self.assertIn(
                tr(locale, "traffic.status.unknown"), draw.call_args.args[2].lines
            )
            self.assertIn(
                tr(
                    locale,
                    "traffic.month_total",
                    month="2026-09",
                    total="3.0 KiB",
                ),
                draw.call_args.args[2].lines,
            )
