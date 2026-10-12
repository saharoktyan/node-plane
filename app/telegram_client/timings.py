"""Opt-in timings without callback payloads, message text or credentials."""
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter
import logging
import os
import re
from uuid import uuid4

from aiogram import BaseMiddleware
from aiogram.client.session.middlewares.base import BaseRequestMiddleware

LOGGER = logging.getLogger(__name__)
CURRENT = ContextVar('node_plane_timing', default=None)


def enabled():
    return os.environ.get('NODE_PLANE_TIMINGS') == '1'


def label(value):
    # IDs and user-supplied arguments never become metric names.
    return re.sub(r'[^a-zA-Z0-9_./-]', '_', value)[:100]


def route(path):
    return '/'.join(':id' if re.fullmatch(r'[0-9a-fA-F-]{36}', part) else part
                    for part in path.split('?')[0].split('/'))


@dataclass
class Measurement:
    id: str = field(default_factory=lambda: uuid4().hex)
    totals: dict = field(default_factory=dict)
    closed: bool = False


def record(stage, started, detail='', request_id='-'):
    if not enabled():
        return
    elapsed = (perf_counter() - started) * 1000
    current = CURRENT.get()
    if current and not current.closed:
        current.totals[stage] = current.totals.get(stage, 0) + elapsed
    LOGGER.info('NP_TIMING action_id=%s stage=%s elapsed_ms=%.3f detail=%s request_id=%s',
                current.id if current and not current.closed else '-', stage, elapsed,
                label(detail), request_id)


class ActionTimings(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if not enabled():
            return await handler(event, data)
        measurement = Measurement()
        token = CURRENT.set(measurement)
        started = perf_counter()
        callback = getattr(event, 'callback_query', None)
        action = label((callback.data or '').split(':')[0]) if callback else 'message'
        outcome = 'error'
        try:
            result = await handler(event, data)
            outcome = 'ok'
            return result
        finally:
            LOGGER.info('NP_TIMING action_id=%s stage=action action=%s outcome=%s total_ms=%.3f stages_ms=%s',
                        measurement.id, action, outcome, (perf_counter() - started) * 1000,
                        {key: round(value, 3) for key, value in measurement.totals.items()})
            measurement.closed = True
            CURRENT.reset(token)


class TelegramTimings(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        # Long polling time is idle waiting, not screen delivery latency.
        if not enabled() or type(method).__name__ == 'GetUpdates':
            return await make_request(bot, method)
        started = perf_counter()
        try:
            return await make_request(bot, method)
        finally:
            record('telegram', started, type(method).__name__)
