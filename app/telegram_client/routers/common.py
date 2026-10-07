from aiogram import BaseMiddleware, Bot
import asyncio
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, TelegramObject
from aiogram.exceptions import TelegramBadRequest, TelegramNotFound, TelegramNetworkError
import logging
import hashlib
from dataclasses import replace
from aiogram.fsm.context import FSMContext
from typing import Callable, Dict, Any, Awaitable

from ..screens import Screen
from ..backend import BackendClient, BackendError
from ..i18n import normalize_locale


def log_rich_failure(stage, exc):
    # Do not log the exception/request object: it may contain config URIs,
    # file uploads or bot credentials. Emit only recognized error categories.
    message = exc.message.lower()
    reason = 'bad_request'
    if isinstance(exc, TelegramNetworkError):
        reason = 'network_timeout' if 'timeout' in message else 'network_error'
    elif isinstance(exc, TelegramNotFound):
        reason = 'not_found'
    else:
        for fragment, category in (
            ('message to edit not found', 'message_missing'),
            ("message can't be edited", 'message_not_editable'),
            ('message is too long', 'message_too_long'),
            ('button_data_invalid', 'invalid_callback'),
            ('button_type_invalid', 'invalid_button'),
            ('unsupported', 'unsupported_content'),
            ('rich', 'invalid_rich_content'),
            ("can't parse", 'invalid_content')):
            if fragment in message:
                reason = category
                break
    logging.getLogger(__name__).warning(
        'Rich screen delivery failed: stage=%s type=%s reason=%s; using fallback',
        stage, type(exc).__name__, reason)


def media_blocks(rich):
    if rich is None or not isinstance(getattr(rich, 'blocks', None), (list, tuple)):
        return
    for block in rich.blocks:
        if getattr(block, 'type', None) in {'document', 'photo'}:
            yield block
        yield from media_blocks(block)


def reuse_media(rich, cache):
    uploads = []
    def rebuild(container):
        blocks = []
        for block in container.blocks:
            if block.type in {'document', 'photo'}:
                item = getattr(block, block.type)
                media = item.media
                if hasattr(media, 'data'):
                    digest = hashlib.sha256(block.type.encode() + media.filename.encode() + media.data).hexdigest()
                    uploads.append((block.type, digest))
                    if digest in cache:
                        block = block.model_copy(update={block.type:
                            item.model_copy(update={'media': cache[digest]})})
            if isinstance(getattr(block, 'blocks', None), (list, tuple)):
                block = rebuild(block)
            blocks.append(block)
        return container.model_copy(update={'blocks': blocks})
    return rebuild(rich), uploads


async def remember_media(state, result, uploads, cache):
    returned = list(media_blocks(getattr(result, 'rich_message', None)))
    if len(returned) != len(uploads):
        return
    for block, (kind, digest) in zip(returned, uploads):
        if block.type != kind:
            return
        item = getattr(block, kind)
        if kind == 'photo':
            item = item[-1] if item else None
        file_id = getattr(item, 'file_id', None)
        if isinstance(file_id, str):
            cache[digest] = file_id
    await state.update_data(rich_media_cache=dict(list(cache.items())[-30:]))

async def render(bot: Bot, chat_id: int, screen: Screen, rows: list[list[InlineKeyboardButton]], state: FSMContext, message_id: int | None = None) -> bool:
    markup = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    fallback_rows = screen.fallback_rows(rows)
    fallback_markup = InlineKeyboardMarkup(inline_keyboard=fallback_rows) if fallback_rows else None
    # Explicitly clear an old inline keyboard when switching from an admin
    # screen or a plain-text fallback to embedded member actions.
    rich_markup = InlineKeyboardMarkup(inline_keyboard=[]) if screen.embedded_buttons else markup
    
    data = await state.get_data()
    # A delayed destructive callback from the previous screen must require a
    # new confirmation, even when navigation edits the same control message.
    if data.get('registry_removal_confirmation') is not None:
        await state.update_data(registry_removal_confirmation=None)
    if data.get('device_delete_confirmation') is not None:
        await state.update_data(device_delete_confirmation=None)
    existing = message_id or data.get('control_message_id')
    rich_content = screen.rich(rows)
    cache = dict(data.get('rich_media_cache', {}))
    rich_content, uploads = reuse_media(rich_content, cache)
    
    if existing:
        try:
            edited = await bot.edit_message_text(chat_id=chat_id, message_id=existing,
                rich_message=rich_content, reply_markup=rich_markup, request_timeout=10)
            await remember_media(state, edited, uploads, cache)
            await state.update_data(control_message_id=existing)
            return True
        except (TelegramBadRequest, TelegramNotFound, TelegramNetworkError) as exc:
            if 'message is not modified' in str(exc).lower():
                return True
            log_rich_failure('edit', exc)
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
            rich_message=rich_content, reply_markup=rich_markup, request_timeout=10)
        await remember_media(state, sent, uploads, cache)
    except (TelegramBadRequest, TelegramNotFound, TelegramNetworkError) as exc:
        rich = False
        log_rich_failure('send', exc)
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
    except (TelegramBadRequest, TelegramNotFound, TelegramNetworkError) as exc:
        log_rich_failure('notice', exc)
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


class LocaleMiddleware(BaseMiddleware):
    """Restore the saved language before routing callbacks after a restart."""

    async def __call__(self, handler, event, data):
        state, backend = data.get('state'), data.get('backend')
        source = getattr(event, 'callback_query', None) or getattr(event, 'message', None)
        user = getattr(source, 'from_user', None)
        if state is not None and backend is not None and user is not None:
            values = await state.get_data()
            if values.get('locale') not in {'en', 'ru'} or values.get('locale_restore_pending'):
                try:
                    account = await asyncio.wait_for(backend.me(user.id), timeout=3)
                except (BackendError, TimeoutError):
                    # Do not cache a fallback as the account's chosen language.
                    # Retry restoration on the next update after backend recovery.
                    await state.update_data(locale=normalize_locale(values.get('locale') or user.language_code),
                                            locale_restore_pending=True)
                else:
                    await state.update_data(locale=normalize_locale(account.get('locale') or
                        account.get('language_code') or user.language_code), locale_restore_pending=False)
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
                locale = (await main.get_data()).get('locale')
                if locale:
                    await isolated.update_data(locale=locale)
                data['state'] = isolated
                data['raw_state'] = await isolated.get_state()
        return await handler(event, data)
