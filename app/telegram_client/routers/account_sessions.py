"""Passwords and sessions in the bot. A password is never entered here, since
secrets do not travel through Telegram: the member sees whether one is set,
lists their sessions and ends them; an administrator sees the same on an
account's card and signs the account out everywhere."""
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton

from ..backend import BackendClient, BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen, Section
from .common import render

router = Router()


def _date(value):
    return datetime.fromisoformat(value).strftime('%Y-%m-%d %H:%M')


def settings_section(locale, user_id, current):
    """The member's password state in Settings, with their sessions."""
    if not current.get('has_password'):
        return Section(tr(locale, 'sessions.title'), (tr(locale, 'sessions.no_password'),))
    return Section(tr(locale, 'sessions.title'), (tr(locale, 'sessions.password_set'),), rows=(
        (InlineKeyboardButton(text=tr(locale, 'sessions.open'), callback_data='msess:list'),),))


def card_lines(locale, account):
    return (tr(locale, 'sessions.card_password' if account.get('has_password') else 'sessions.card_no_password'),
            tr(locale, 'sessions.card_count', count=account.get('sessions', 0)))


def card_rows(locale, account):
    if not account.get('sessions'):
        return []
    return [[InlineKeyboardButton(text=tr(locale, 'sessions.end_account'), callback_data='asess:' + account['id'])]]


async def show_sessions(chat_id, user_id, message_id, bot, backend, state, notice=None):
    locale = normalize_locale((await state.get_data()).get('locale'))
    items = (await backend.request('GET', '/api/v1/me/sessions', telegram_user_id=user_id))['items']
    lines = ((notice,) if notice else ()) + tuple(
        tr(locale, 'sessions.item', device=item['device'], used=_date(item['used_at'])) for item in items
    ) or (tr(locale, 'sessions.empty'),)
    from .user import button
    rows = [[InlineKeyboardButton(text=tr(locale, 'sessions.end', device=item['device'][:40]),
                                  callback_data='msess:end:' + item['id'])] for item in items]
    if len(items) > 1:
        rows.append([InlineKeyboardButton(text=tr(locale, 'sessions.end_all'), callback_data='msess:all')])
    rows.append([button(user_id, tr(locale, 'back'), 'member_settings')])
    await render(bot, chat_id, Screen(tr(locale, 'sessions.title'), lines, embedded_buttons=True, navigation=True),
                 rows, state, message_id)


@router.callback_query(F.data.startswith('msess:'))
async def sessions_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    chat_id, user_id, message_id = query.message.chat.id, query.from_user.id, query.message.message_id
    action = query.data.removeprefix('msess:')
    notice = None
    try:
        if action == 'all':
            # The bot is no session of its own, so this ends all of them.
            await backend.request('DELETE', '/api/v1/me/sessions', telegram_user_id=user_id)
        elif action.startswith('end:'):
            await backend.request('DELETE', '/api/v1/me/sessions/' + action.removeprefix('end:'),
                                  telegram_user_id=user_id)
    except BackendError as exc:
        from .admin_settings import _friendly_error
        notice = _friendly_error(normalize_locale((await state.get_data()).get('locale')), exc)
    await show_sessions(chat_id, user_id, message_id, bot, backend, state, notice)


@router.callback_query(F.data.startswith('asess:'))
async def account_sessions_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    account_id = query.data.removeprefix('asess:')
    await backend.request('DELETE', f'/api/v1/accounts/{account_id}/sessions', telegram_user_id=query.from_user.id,
                          command=True)
    from .admin_profiles import show_account
    await show_account(query.message.chat.id, query.from_user.id, query.message.message_id, account_id,
                       bot, backend, state)
