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
        from .release_notes import preview
        changelog = payload.get('changelog')
        blocks,lines = preview(changelog or '')
        return Screen(tr(locale, 'updates.notice.title'),
            (tr(locale, 'updates.notice.version', value=payload['version']),
             tr(locale, 'updates.notice.branch', value=payload['branch'])),
            sections=(Section(tr(locale, 'updates.notice.changelog'),
                lines=lines, formatted_blocks=blocks, collapsed=True),) if blocks else (),
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
        options['rows'] = []
        url = delivery['event']['payload'].get('release_url')
        if url and url.startswith('https://github.com/'):
            options['rows'].append([InlineKeyboardButton(text=tr(normalize_locale(delivery.get('locale')),'updates.notice.full_release'),url=url)])
        options['rows'].append([InlineKeyboardButton(text=tr(normalize_locale(delivery.get('locale')), 'setup.close'), callback_data='update_notice_close')])
    status = await send_notification(bot, delivery["telegram_user_id"], alert_screen(delivery), **options)
    options = {'retry_after': status.retry_after} if hasattr(status, 'retry_after') else {}
    await backend.alert_ack(delivery["id"], key, status, **options)
    return True
