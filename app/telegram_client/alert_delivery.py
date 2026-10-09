"""Localized transport for backend alert events, independent of Telegram UI state."""

from uuid import uuid4
from aiogram.types import InlineKeyboardButton

from .i18n import normalize_locale, tr
from .notification_transport import send_notification
from .screens import Screen, Section


def alert_screen(delivery):
    locale = normalize_locale(delivery.get("locale"))
    event = delivery["event"]
    if event['kind'] == 'update_available':
        payload = event['payload']
        changelog = payload.get('changelog')
        return Screen(tr(locale, 'updates.notice.title'),
            (tr(locale, 'updates.notice.version', value=payload['version']),
             tr(locale, 'updates.notice.branch', value=payload['branch'])),
            sections=(Section(tr(locale, 'updates.notice.changelog'),
                lines=(changelog,), collapsed=True),) if changelog else (),
            embedded_buttons=True)
    lines = [
        tr(locale, "alerts.node", title=event["payload"]["node_title"]),
        tr(locale, "alerts.condition." + event["kind"]),
        tr(locale, "alerts.observed", at=event["at"][:19].replace("T", " ")),
    ]
    if "value" in event["payload"]:
        lines.append(tr(locale, "alerts.value", value=event["payload"]["value"]))
    return Screen(
        tr(locale, "alerts.recovered" if event["resolved"] else "alerts.problem"),
        tuple(lines),
    )


async def deliver_one(bot, backend):
    key = str(uuid4())
    value = await backend.alert_claim(key)
    delivery = value.get("delivery")
    if not delivery:
        return False
    options = {}
    if delivery['event']['kind'] == 'update_available':
        options['rows'] = [[InlineKeyboardButton(text=tr(normalize_locale(delivery.get('locale')), 'setup.close'),
            callback_data='update_notice_close')]]
    status = await send_notification(bot, delivery["telegram_user_id"], alert_screen(delivery), **options)
    await backend.alert_ack(delivery["id"], key, status)
    return True
