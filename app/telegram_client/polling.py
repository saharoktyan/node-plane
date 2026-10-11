"""Stop competing Telegram pollers instead of alternating controller state."""
import asyncio
from contextlib import suppress
import json
import logging
from pathlib import Path

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramConflictError
from aiogram.methods import GetUpdates


CONFLICT_EXIT_CODE = 78
CONFLICT_MESSAGE = ('Another process is receiving updates for this Telegram bot token. '
                    'Stop the bot on the other installation, then restart node-plane-telegram.service.')


class BotTokenConflict(RuntimeError):
    pass


class PollingConflictGuard(BaseRequestMiddleware):
    def __init__(self):
        self.conflict = asyncio.Event()

    async def __call__(self, make_request, bot, method):
        try:
            return await make_request(bot, method)
        except TelegramConflictError:
            if isinstance(method, GetUpdates):
                self.conflict.set()
            raise


async def guarded_polling(dispatcher, bot, status_file: Path):
    guard = PollingConflictGuard()
    # A manual restart permits a fresh check after the competing process is stopped.
    status_file.unlink(missing_ok=True)
    bot.session.middleware(guard)
    polling = asyncio.create_task(dispatcher.start_polling(bot))
    conflict = asyncio.create_task(guard.conflict.wait())
    try:
        await asyncio.wait((polling, conflict), return_when=asyncio.FIRST_COMPLETED)
        if guard.conflict.is_set():
            try:
                status_file.parent.mkdir(parents=True, exist_ok=True)
                status_file.write_text(json.dumps({'code': 'telegram_polling_conflict'}), encoding='utf-8')
            except OSError:
                logging.getLogger(__name__).error('Could not persist Telegram polling conflict status')
            logging.getLogger(__name__).critical(CONFLICT_MESSAGE)
            # Let aiogram stop and await its polling tasks before closing the session.
            if not polling.done():
                await dispatcher.stop_polling()
            await polling
            raise BotTokenConflict(CONFLICT_MESSAGE)
        await polling
    finally:
        conflict.cancel()
        with suppress(asyncio.CancelledError):
            await conflict
        if not polling.done():
            await dispatcher.stop_polling()
            await polling
        bot.session.middleware.unregister(guard)
