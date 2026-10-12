"""Overlap empty callback acknowledgements with loading and screen delivery."""
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
import logging

from aiogram import BaseMiddleware
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import AnswerCallbackQuery

PENDING = ContextVar('node_plane_callback_answers', default=None)


@dataclass
class PendingAnswers:
    tasks: list = field(default_factory=list)
    closed: bool = False


class CallbackAnswerScope(BaseMiddleware):
    async def __call__(self, handler, event, data):
        pending = PendingAnswers()
        token = PENDING.set(pending)
        try:
            return await handler(event, data)
        except asyncio.CancelledError:
            for task in pending.tasks:
                task.cancel()
            raise
        finally:
            # Keep requests inside the update lifetime: no orphaned tasks on
            # errors or shutdown, and timing summaries include the real calls.
            try:
                pending.closed = True
                if pending.tasks:
                    results = await asyncio.gather(*pending.tasks, return_exceptions=True)
                    for result in results:
                        if isinstance(result, BaseException):
                            raise result
            finally:
                pending.closed = True
                PENDING.reset(token)


class ConcurrentCallbackAnswers(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        pending = PENDING.get()
        if (pending is None or pending.closed or not isinstance(method, AnswerCallbackQuery)
                or method.text or method.show_alert or method.url):
            return await make_request(bot, method)

        async def acknowledge():
            try:
                await make_request(bot, method)
            except TelegramAPIError as exc:
                # A failed empty acknowledgement must not discard a screen
                # already delivered successfully. Never log Telegram payloads.
                logging.getLogger(__name__).warning(
                    'Empty callback acknowledgement failed: type=%s', type(exc).__name__)

        pending.tasks.append(asyncio.create_task(acknowledge()))
        return True
