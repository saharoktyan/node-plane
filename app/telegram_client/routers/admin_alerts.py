"""Alert policy and active conditions backed by the monitoring service."""

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton

from ..backend import BackendClient, BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen, server_label
from .callbacks import AdminSettingsCallback
from .common import render

router = Router()


def button(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


@router.callback_query(F.data == "alerts")
@router.callback_query(F.data.startswith("alert_pref:"))
@router.callback_query(F.data.startswith("alerts_active:"))
async def alerts_cb(
    query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext
):
    await query.answer()
    locale = normalize_locale((await state.get_data()).get("locale"))
    try:
        value = await backend.alerts_overview(query.from_user.id)
        if query.data.startswith("alert_pref:"):
            field = query.data.split(":")[1]
            change = {
                field: int(query.data.split(":")[2])
                if field == "interval_minutes"
                else not value[field]
            }
            await backend.alert_preferences(query.from_user.id, change)
            value = await backend.alerts_overview(query.from_user.id)
        if query.data.startswith("alerts_active:"):
            offset = max(0, int(query.data.split(":")[1]))
            items = value["active"][offset : offset + 8]
            lines = [
                tr(
                    locale,
                    "alerts.active_item",
                    title=server_label(item),
                    condition=tr(locale, "alerts.condition." + item["kind"]),
                )
                for item in items
            ]
            nav = []
            if offset:
                nav.append(button("‹", f"alerts_active:{max(0, offset - 8)}"))
            if offset + 8 < len(value["active"]):
                nav.append(button("›", f"alerts_active:{offset + 8}"))
            rows = [nav] if nav else []
            rows.append([button(tr(locale, "back"), "alerts")])
            await render(
                bot,
                query.message.chat.id,
                Screen(
                    tr(locale, "alerts.active_title"),
                    tuple(lines or [tr(locale, "alerts.none")]),
                ),
                rows,
                state,
                query.message.message_id,
            )
            return
        last = value.get("last_scan") or {}
        lines = [
            tr(locale, "alerts.active_count", count=value["active_count"]),
            tr(locale, "alerts.interval", minutes=value["interval_minutes"]),
            tr(
                locale,
                "alerts.last",
                at=(last.get("at") or "—")[:19],
                status=tr(locale, "alerts.status." + last.get("status", "never")),
            ),
            tr(locale, "alerts.thresholds"),
            tr(locale, "alerts.unknown_note"),
        ]
        counts = value["delivery_counts"]
        lines.append(
            tr(
                locale,
                "alerts.deliveries",
                failed=counts["failed"],
                unknown=counts["unknown"],
                queued=counts["queued"] + counts["claimed"],
            )
        )
        rows = [
            [
                button(
                    tr(
                        locale,
                        "alerts.enabled" if value["enabled"] else "alerts.disabled",
                    ),
                    "alert_pref:enabled",
                )
            ],
            [
                button(
                    ("✅ " if value["interval_minutes"] == minutes else "")
                    + tr(locale, "alerts.minutes", minutes=minutes),
                    f"alert_pref:interval_minutes:{minutes}",
                )
                for minutes in (5, 15)
            ],
            [
                button(
                    tr(
                        locale,
                        "alerts.resolved_on"
                        if value["notify_resolved"]
                        else "alerts.resolved_off",
                    ),
                    "alert_pref:notify_resolved",
                )
            ],
            [
                button(tr(locale, "alerts.active_title"), "alerts_active:0"),
                button(tr(locale, "updates.refresh"), "alerts"),
            ],
            [button(tr(locale, "back"), AdminSettingsCallback().pack())],
        ]
        await render(
            bot,
            query.message.chat.id,
            Screen(tr(locale, "alerts.title"), tuple(lines)),
            rows,
            state,
            query.message.message_id,
        )
    except (BackendError, ValueError, KeyError):
        await render(
            bot,
            query.message.chat.id,
            Screen(tr(locale, "alerts.title"), (tr(locale, "alerts.error"),)),
            [[button(tr(locale, "back"), AdminSettingsCallback().pack())]],
            state,
            query.message.message_id,
        )
