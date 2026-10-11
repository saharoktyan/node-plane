"""Telegram transport for backend-owned announcement deliveries."""

import asyncio
import logging
from uuid import uuid4
from aiogram.types import InlineKeyboardButton

from .i18n import tr
from .notification_transport import send_notification
from .screens import Screen


async def deliver_one(bot, backend):
    key = str(uuid4())
    value = await backend.announcement_claim(key)
    delivery = value.get("delivery")
    if not delivery:
        return False
    screen = Screen(
        tr(delivery.get("locale"), "announce.notification"), (delivery["text"],)
    )
    rows = [[InlineKeyboardButton(text=tr(delivery.get("locale"), "announce.close"),
        callback_data="announce_close:" + delivery["id"])]]
    status = await send_notification(
        bot, delivery["telegram_user_id"], screen, delivery["silent"], rows=rows
    )
    await backend.announcement_ack(delivery["id"], key, status)
    return True


async def delivery_loop(bot, backend):
    from .alert_delivery import deliver_one as deliver_alert

    while True:
        worked = False
        for delivery in (deliver_alert, deliver_one):
            try:
                worked = await delivery(bot, backend) or worked
            except Exception:  # noqa: BLE001 -- continue independent transports
                logging.getLogger(__name__).warning(
                    "Notification delivery requires attention"
                )
        if worked:
            await asyncio.sleep(0.1)
        else:
            try:
                await backend.wait_deliveries()
            except Exception:
                # Includes older controllers and interrupted long polls.
                await asyncio.sleep(5)
