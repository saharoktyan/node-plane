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


async def refresh_menu(bot, backend, dispatcher):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from .routers.user import show_home
    delivery = (await backend.request('POST', '/api/v1/integrations/telegram/menu-refresh/claim')).get('delivery')
    if not delivery:
        return False
    account = delivery['account']
    recipient = account.get('telegram_user_id')
    if recipient:
        key = StorageKey(bot_id=bot.id, chat_id=recipient, user_id=recipient)
        async with dispatcher.fsm.events_isolation.lock(key=key):
            state = FSMContext(storage=dispatcher.storage, key=key)
            data = await state.get_data()
            await state.update_data(home_presentation=None)
            parents = (data.get('navigation_screen') or {}).get('parents') or []
            if data.get('control_message_id') and len(parents) == 1 and str(parents[0].get('callback', '')).startswith('u:'):
                await show_home(recipient, recipient, bot, backend, state, data['control_message_id'], account=account)
    await backend.request('POST', f"/api/v1/integrations/telegram/menu-refresh/{account['id']}/ack?revision={delivery['revision']}")
    return True


async def delivery_loop(bot, backend, dispatcher=None):
    from .alert_delivery import deliver_one as deliver_alert

    while True:
        worked = False
        refresh_failed = False
        if dispatcher is not None:
            try:
                worked = await refresh_menu(bot, backend, dispatcher)
            except Exception:
                refresh_failed = True
                logging.getLogger(__name__).warning("Account menu refresh requires attention")
        for delivery in (deliver_alert, deliver_one):
            try:
                worked = await delivery(bot, backend) or worked
            except Exception:  # noqa: BLE001 -- continue independent transports
                logging.getLogger(__name__).warning(
                    "Notification delivery requires attention"
                )
        if refresh_failed:
            # A queued refresh keeps the wait endpoint ready; avoid a tight retry loop.
            await asyncio.sleep(5)
        elif worked:
            await asyncio.sleep(0.1)
        else:
            try:
                await backend.wait_deliveries()
            except Exception:
                # Includes older controllers and interrupted long polls.
                await asyncio.sleep(5)
