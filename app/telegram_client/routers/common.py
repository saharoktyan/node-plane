from aiogram import BaseMiddleware, Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, TelegramObject
from aiogram.exceptions import TelegramBadRequest, TelegramNotFound, TelegramNetworkError
import logging
from dataclasses import replace
from aiogram.fsm.context import FSMContext
from typing import Callable, Dict, Any, Awaitable

from ..screens import Screen
from ..backend import BackendClient

async def render(bot: Bot, chat_id: int, screen: Screen, rows: list[list[InlineKeyboardButton]], state: FSMContext, message_id: int | None = None) -> bool:
    markup = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    fallback_rows = screen.fallback_rows(rows)
    fallback_markup = InlineKeyboardMarkup(inline_keyboard=fallback_rows) if fallback_rows else None
    # Explicitly clear an old inline keyboard when switching from an admin
    # screen or a plain-text fallback to embedded member actions.
    rich_markup = InlineKeyboardMarkup(inline_keyboard=[]) if screen.embedded_buttons else markup
    
    data = await state.get_data()
    existing = message_id or data.get('control_message_id')
    
    if existing:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=existing,
                rich_message=screen.rich(rows), reply_markup=rich_markup, request_timeout=10)
            await state.update_data(control_message_id=existing)
            return True
        except (TelegramBadRequest, TelegramNotFound, TelegramNetworkError) as exc:
            if 'message is not modified' in str(exc).lower():
                return True
            try:
                await bot.edit_message_text(chat_id=chat_id,
                    message_id=existing, text=screen.plain(), entities=screen.plain_entities(), reply_markup=fallback_markup, request_timeout=15)
                await state.update_data(control_message_id=existing)
                return False
            except (TelegramBadRequest, TelegramNotFound):
                pass
                
    rich = True
    try:
        sent = await bot.send_rich_message(chat_id=chat_id,
            rich_message=screen.rich(rows), reply_markup=rich_markup, request_timeout=10)
    except (TelegramBadRequest, TelegramNotFound, TelegramNetworkError) as exc:
        rich = False
        logging.getLogger(__name__).warning('Rich screen delivery failed (%s); using plain text', type(exc).__name__)
        sent = await bot.send_message(chat_id=chat_id,
            text=screen.plain(), entities=screen.plain_entities(), reply_markup=fallback_markup, request_timeout=15)
            
    await state.update_data(control_message_id=sent.message_id)
    if data.get('notification_session') and existing != sent.message_id:
        # A missing notice may be recreated by the delivery fallback. Its new
        # callbacks must still use an isolated FSM, never the main panel.
        replacement = FSMContext(storage=state.storage,
            key=replace(state.key, destiny=f'notification:{sent.message_id}'))
        await replacement.set_data(await state.get_data())
        await replacement.set_state(await state.get_state())
        await state.update_data(notification_closed=True)
    return rich

async def send_notice(bot: Bot, chat_id: int, screen: Screen, markup: InlineKeyboardMarkup | None = None) -> None:
    rows = markup.inline_keyboard if markup else []
    rich_markup = InlineKeyboardMarkup(inline_keyboard=[]) if screen.embedded_buttons else markup
    try:
        await bot.send_rich_message(chat_id=chat_id, rich_message=screen.rich(rows), reply_markup=rich_markup, request_timeout=10)
    except (TelegramBadRequest, TelegramNotFound, TelegramNetworkError):
        fallback_rows = screen.fallback_rows(rows)
        fallback_markup = InlineKeyboardMarkup(inline_keyboard=fallback_rows) if fallback_rows else None
        await bot.send_message(chat_id=chat_id, text=screen.plain(), reply_markup=fallback_markup, request_timeout=15)

class BackendMiddleware(BaseMiddleware):
    def __init__(self, backend: BackendClient):
        self.backend = backend

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        data["backend"] = self.backend
        return await handler(event, data)


class NotificationStateMiddleware(BaseMiddleware):
    """Keep notification workflows separate from the user's main panel FSM."""

    async def __call__(self, handler, event, data):
        main = data.get('state')
        query = getattr(event, 'callback_query', None)
        message = getattr(event, 'message', None)
        target = query.message if query else getattr(message, 'reply_to_message', None)
        if main is not None and target is not None:
            isolated = FSMContext(storage=main.storage,
                key=replace(main.key, destiny=f'notification:{target.message_id}'))
            values = await isolated.get_data()
            initial = query and (query.data or '').startswith(('notification_',))
            if initial and not values.get('notification_session'):
                await isolated.update_data(notification_session=True,
                    control_message_id=target.message_id,
                    locale=(await main.get_data()).get('locale') or query.from_user.language_code)
                values = await isolated.get_data()
            if values.get('notification_session'):
                if values.get('notification_closed'):
                    if query:
                        await query.answer()
                    return
                data['state'] = isolated
                data['raw_state'] = await isolated.get_state()
        return await handler(event, data)
