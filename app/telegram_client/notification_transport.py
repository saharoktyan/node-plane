"""Single-attempt notification delivery with safe Rich Message fallback."""

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardMarkup


class DeferredDelivery(str):
    def __new__(cls, retry_after):
        value = super().__new__(cls, 'retry')
        value.retry_after = max(1, min(int(retry_after), 86400))
        return value


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
    except TelegramRetryAfter as error:
        return DeferredDelivery(error.retry_after)
    except (TelegramBadRequest, TelegramForbiddenError):
        return "failed"
    except Exception:  # noqa: BLE001 -- never replay an ambiguous send
        return "unknown"
