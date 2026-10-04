from aiogram import BaseMiddleware, Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, TelegramObject
from aiogram.exceptions import TelegramBadRequest, TelegramNotFound, TelegramNetworkError
import logging
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
