"""Update every known administrator notice after a committed access decision."""
import asyncio

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNotFound, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .i18n import normalize_locale, tr
from .screens import Screen


def decision_text(locale, delivery):
    actor = delivery['decider']
    label = ('@' + actor['username'].removeprefix('@') if actor.get('username') else
             ' '.join(filter(None, (actor.get('first_name'), actor.get('last_name')))) or
             str(actor.get('telegram_user_id') or actor['id']))
    return tr(locale, 'requests.decision.' + delivery['decision'], actor=label) + (
        tr(locale, 'requests.via_workstation') if delivery['surface'] == 'workstation' else '')


async def deliver_one(bot, backend, dispatcher=None):
    delivery = (await backend.request('POST', '/api/v1/integrations/telegram/access-decisions/claim')).get('delivery')
    if not delivery:
        return False
    if dispatcher is not None:
        from dataclasses import replace
        from aiogram.fsm.context import FSMContext
        from aiogram.fsm.storage.base import StorageKey
        main_key = StorageKey(bot_id=bot.id, chat_id=delivery['chat_id'], user_id=delivery['chat_id'])
        key = replace(main_key, destiny=f"notification:{delivery['message_id']}")
        # Dispatcher acquires the main key before notification middleware swaps
        # its FSM destiny. Use the same lock as callbacks to serialize decisions.
        async with dispatcher.fsm.events_isolation.lock(key=main_key):
            state = FSMContext(storage=dispatcher.storage, key=key)
            values = await state.get_data()
            # The deciding administrator can already be editing the approved
            # profile in this notice. Never replace that workflow with a receipt.
            if not (values.get('notification_closed') or values.get('notification_result')
                    or values.get('profile_setup')):
                if not await edit_notice(bot, delivery):
                    return False
            await acknowledge(backend, delivery)
            return True
    if not await edit_notice(bot, delivery):
        return False
    await acknowledge(backend, delivery)
    return True


async def edit_notice(bot, delivery):
    locale = normalize_locale(delivery['locale'])
    screen = Screen(tr(locale, 'requests.title'), (decision_text(locale, delivery),), embedded_buttons=True)
    rows = [[InlineKeyboardButton(text=tr(locale, 'setup.close'), callback_data='notification_close')]]
    options = {'chat_id': delivery['chat_id'], 'message_id': delivery['message_id'], 'request_timeout': 15}
    try:
        try:
            await bot.edit_message_rich_message(**options, rich_message=screen.rich(rows),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[]))
        except TelegramBadRequest as error:
            if 'not modified' not in str(error).lower():
                await bot.edit_message_text(**options, text=screen.plain(),
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except TelegramRetryAfter as error:
        await asyncio.sleep(max(1, min(error.retry_after, 86400)))
        return False
    except (TelegramBadRequest, TelegramForbiddenError, TelegramNotFound) as error:
        if not any(text in str(error).lower() for text in ('not found', 'not modified', 'blocked', 'chat not found', 'deactivated')):
            raise
    return True


async def acknowledge(backend, delivery):
    await backend.request('POST',
        f"/api/v1/integrations/telegram/access-decisions/{delivery['request_id']}/ack?chat_id={delivery['chat_id']}&message_id={delivery['message_id']}", body={})
