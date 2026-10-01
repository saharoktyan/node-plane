"""Single-attempt notification delivery with safe Rich Message fallback."""

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)


async def send_notification(bot, chat_id, screen, silent=False):
    try:
        try:
            await bot.send_rich_message(
                chat_id=chat_id, rich_message=screen.rich(), disable_notification=silent
            )
        except TelegramBadRequest:
            await bot.send_message(
                chat_id=chat_id, text=screen.plain(), disable_notification=silent
            )
        return "sent"
    except (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter):
        return "failed"
    except Exception:  # noqa: BLE001 -- never replay an ambiguous send
        return "unknown"
