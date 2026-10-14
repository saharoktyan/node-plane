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
    sender = delivery.get("sender_username")
    screen = Screen(
        tr(delivery.get("locale"), "announce.notification"),
        (("by @" + sender.lstrip("@"),) if sender else ()) + (delivery["text"],),
        embedded_buttons=True
    )
    rows = [[InlineKeyboardButton(text=tr(delivery.get("locale"), "announce.close"),
        callback_data="announce_close:" + delivery["id"]),
        InlineKeyboardButton(text=tr(delivery.get("locale"), "announce.dismiss"),
        callback_data="announce_dismiss:" + delivery["id"])]]
    status = await send_notification(
        bot, delivery["telegram_user_id"], screen, delivery["silent"], rows=rows
    )
    options = {'retry_after': status.retry_after} if hasattr(status, 'retry_after') else {}
    await backend.announcement_ack(delivery["id"], key, status, **options)
    return True


async def refresh_menu(bot, backend, dispatcher):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from .routers.user import show_home
    from .access_guard import leave_admin_panel, admin_panel
    delivery = (await backend.request('POST', '/api/v1/integrations/telegram/menu-refresh/claim')).get('delivery')
    if not delivery:
        return False
    account = delivery['account']
    original_account_id = account['id']
    recipient = account.get('telegram_user_id')
    if recipient:
        key = StorageKey(bot_id=bot.id, chat_id=recipient, user_id=recipient)
        async with dispatcher.fsm.events_isolation.lock(key=key):
            state = FSMContext(storage=dispatcher.storage, key=key)
            data = await state.get_data()
            if account.get('deleted') and data.get('control_message_id'):
                await backend.resolve(recipient, username=account.get('username'),
                    first_name=account.get('first_name'), last_name=account.get('last_name'),
                    language_code=account.get('language_code'))
                account = await backend.me(recipient)
                await leave_admin_panel(bot, backend, state, recipient, recipient,
                    data['control_message_id'], account)
                await backend.request('POST', f"/api/v1/integrations/telegram/menu-refresh/{original_account_id}/ack?revision={delivery['revision']}")
                return True
            previous = (data.get('home_presentation') or {}).get('account') or {}
            lost_access = (account.get('role') != 'admin' or account.get('status') != 'approved') and (
                previous.get('role') == 'admin' or admin_panel(data))
            await state.update_data(home_presentation=None)
            parents = (data.get('navigation_screen') or {}).get('parents') or []
            if data.get('control_message_id') and lost_access:
                await leave_admin_panel(bot, backend, state, recipient, recipient,
                    data['control_message_id'], account)
            elif data.get('control_message_id') and (data.get('home_panel_active')
                    or 'home_panel_active' not in data and len(parents) == 1
                    and str(parents[0].get('callback', '')).startswith('u:')):
                try:
                    await show_home(recipient, recipient, bot, backend, state, data['control_message_id'], account=account)
                except Exception:
                    # Rendering clears the screen marker before Telegram accepts the edit.
                    # Keep this queued home refresh retryable if delivery fails.
                    await state.update_data(home_panel_active=True)
                    raise
    await backend.request('POST', f"/api/v1/integrations/telegram/menu-refresh/{account['id']}/ack?revision={delivery['revision']}")
    return True


async def delivery_loop(bot, backend, dispatcher=None):
    from .alert_delivery import deliver_one as deliver_alert
    from .access_decision_delivery import deliver_one as deliver_access_decision

    while True:
        worked = False
        refresh_failed = False
        if dispatcher is not None:
            try:
                worked = await refresh_menu(bot, backend, dispatcher)
            except Exception:
                refresh_failed = True
                logging.getLogger(__name__).warning("Account menu refresh requires attention")
        for delivery in (deliver_access_decision, deliver_alert, deliver_one):
            try:
                result = (await delivery(bot, backend, dispatcher)
                          if delivery is deliver_access_decision else await delivery(bot, backend))
                worked = result or worked
            except Exception:  # noqa: BLE001 -- continue independent transports
                refresh_failed = True
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
                hint = await backend.wait_deliveries()
                if isinstance(hint, dict) and hint.get('retry_after'):
                    await asyncio.sleep(min(hint['retry_after'], 60))
            except Exception:
                # Includes older controllers and interrupted long polls.
                await asyncio.sleep(5)
