from aiogram import BaseMiddleware, Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery, TelegramObject
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from typing import Callable, Dict, Any, Awaitable
import time

from ..screens import Screen
from ..backend import BackendClient

async def render(bot: Bot, chat_id: int, screen: Screen, rows: list[list[InlineKeyboardButton]], state: FSMContext, message_id: int | None = None) -> None:
    markup = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    
    data = await state.get_data()
    existing = message_id or data.get('control_message_id')
    
    if existing:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=existing,
                rich_message=screen.rich(), reply_markup=markup)
            await state.update_data(control_message_id=existing)
            return
        except TelegramBadRequest as exc:
            if 'message is not modified' in str(exc).lower():
                return
            try:
                await bot.edit_message_text(chat_id=chat_id,
                    message_id=existing, text=screen.plain(), reply_markup=markup)
                await state.update_data(control_message_id=existing)
                return
            except TelegramBadRequest:
                pass
                
    try:
        sent = await bot.send_rich_message(chat_id=chat_id,
            rich_message=screen.rich(), reply_markup=markup)
    except TelegramBadRequest:
        sent = await bot.send_message(chat_id=chat_id,
            text=screen.plain(), reply_markup=markup)
            
    await state.update_data(control_message_id=sent.message_id)

async def send_notice(bot: Bot, chat_id: int, screen: Screen, markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await bot.send_rich_message(chat_id=chat_id, rich_message=screen.rich(), reply_markup=markup)
    except TelegramBadRequest:
        await bot.send_message(chat_id=chat_id, text=screen.plain(), reply_markup=markup)

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
