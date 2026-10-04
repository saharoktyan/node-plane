"""Localized traffic settings and member summaries on the single-message UI."""

from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.i18n import tr
from telegram_client.routers import admin_settings, user

from tests import test_telegram_client as fixture


class TelegramTrafficTests(IsolatedAsyncioTestCase):
    setUp = fixture.TelegramFlowTests.setUp

    async def test_member_consent_hidden_when_unavailable_but_still_revocable(self):
        for locale in ("ru", "en"):
            self.state_data["locale"] = locale
            for available, consent, visible in (
                (False, False, False),
                (True, False, True),
                (False, True, True),
            ):
                backend = SimpleNamespace(
                    me=AsyncMock(
                        return_value={
                            "traffic_available": available,
                            "traffic_consent": consent,
                        }
                    )
                )
                with patch.object(user, "render", new_callable=AsyncMock) as draw:
                    await user.show_member_settings(
                        123, 123, 77, self.bot, backend, self.state
                    )
                screen = draw.call_args.args[2]
                labels = [b.text for row in screen.fallback_rows(draw.call_args.args[3]) for b in row]
                expected = tr(
                    locale, "ui.withdraw_consent" if consent else "ui.give_consent"
                )
                self.assertEqual(expected in labels, visible)
                self.assertEqual(draw.call_args.args[-1], 77)

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
            self.assertEqual(draw.call_args.args[3][0][0].callback_data, "traffic:off")
            self.assertIn(
                tr(locale, "traffic.scan", at="2026-09-30 12:00", checked=3, unknown=1),
                draw.call_args.args[2].lines,
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
