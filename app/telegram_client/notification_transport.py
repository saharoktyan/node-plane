"""Single-attempt notification delivery with safe Rich Message fallback."""

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardMarkup


async def send_notification(bot, chat_id, screen, silent=False, *, rows=None):
    try:
        try:
            await bot.send_rich_message(
                chat_id=chat_id, rich_message=screen.rich(rows), disable_notification=silent
            )
        except TelegramBadRequest:
            options = {'reply_markup': InlineKeyboardMarkup(inline_keyboard=rows)} if rows else {}
            await bot.send_message(chat_id=chat_id, text=screen.plain(),
                disable_notification=silent, **options)
        return "sent"
    except (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter):
        return "failed"
    except Exception:  # noqa: BLE001 -- never replay an ambiguous send
        return "unknown"
