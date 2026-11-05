"""Logins: the member sets or changes their own in Settings; an administrator
changes any account's login from its card."""
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from ..backend import BackendClient, BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen, Section
from .common import render

router = Router()

ERRORS = {'login_invalid', 'login_reserved', 'login_taken', 'login_change_too_soon'}


class LoginState(StatesGroup):
    waiting_for_login = State()


def _date(value):
    return datetime.fromisoformat(value).strftime('%Y-%m-%d')


def settings_section(locale, user_id, current):
    """The member's login in Settings, with setting it or the Telegram username."""
    login = current.get('login')
    lines = [tr(locale, 'login.current', value=login) if login else tr(locale, 'login.none')]
    rows = []
    available_at = current.get('login_change_available_at')
    if available_at:
        lines.append(tr(locale, 'login.change_after', date=_date(available_at)))
    else:
        rows.append((InlineKeyboardButton(text=tr(locale, 'login.change' if login else 'login.set'),
                                          callback_data='mlogin:edit'),))
    if current.get('login_suggestion'):
        rows.append((InlineKeyboardButton(text=tr(locale, 'login.use', value=current['login_suggestion']),
                                          callback_data='mlogin:use', style='primary'),))
    return Section(tr(locale, 'login.title'), tuple(lines), rows=tuple(rows))


def card_lines(locale, account):
    return (tr(locale, 'login.current', value=account['login']) if account.get('login')
            else tr(locale, 'login.none_admin'),)


def card_row(locale, account):
    return [InlineKeyboardButton(text=tr(locale, 'login.change_admin'), callback_data='alogin:' + account['id'])]


def error_text(locale, exc):
    from .admin_settings import _friendly_error
    return tr(locale, 'login.error.' + exc.code) if exc.code in ERRORS else _friendly_error(locale, exc)


async def _prompt(chat_id, message_id, bot, state, locale, back, notice=None):
    lines = ((notice,) if notice else ()) + (tr(locale, 'login.prompt'),)
    await render(bot, chat_id, Screen(tr(locale, 'login.title'), lines, embedded_buttons=True, navigation=True),
                 [[back]], state, message_id)


async def _back(locale, user_id, account_id):
    if account_id:
        return InlineKeyboardButton(text=tr(locale, 'back'), callback_data='account:' + account_id)
    from .user import button
    return button(user_id, tr(locale, 'back'), 'member_settings')


async def _done(chat_id, user_id, message_id, account_id, bot, backend, state):
    if account_id:
        from .admin_profiles import show_account
        return await show_account(chat_id, user_id, message_id, account_id, bot, backend, state)
    from .user import show_member_settings
    await show_member_settings(chat_id, user_id, message_id, bot, backend, state)


async def _save(user_id, account_id, login, backend):
    if account_id:
        account = await backend.request('GET', f'/api/v1/accounts/{account_id}', telegram_user_id=user_id)
        await backend.request('PATCH', f'/api/v1/accounts/{account_id}', telegram_user_id=user_id, command=True,
                              revision=account['revision'], body={'login': login})
    else:
        await backend.request('PUT', '/api/v1/me/login', telegram_user_id=user_id, body={'login': login})


@router.callback_query(F.data.in_({'mlogin:edit', 'mlogin:use'}) | F.data.startswith('alogin:'))
async def login_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    locale = normalize_locale((await state.get_data()).get('locale'))
    chat_id, user_id, message_id = query.message.chat.id, query.from_user.id, query.message.message_id
    account_id = query.data.removeprefix('alogin:') if query.data.startswith('alogin:') else None
    back = await _back(locale, user_id, account_id)
    if query.data == 'mlogin:use':
        current = await backend.me(user_id)
        if current.get('login_suggestion'):
            try:
                await _save(user_id, None, current['login_suggestion'], backend)
            except BackendError as exc:
                return await _prompt(chat_id, message_id, bot, state, locale, back, error_text(locale, exc))
        return await _done(chat_id, user_id, message_id, None, bot, backend, state)
    await state.set_state(LoginState.waiting_for_login)
    await state.update_data(login_input={'account_id': account_id, 'message_id': message_id})
    await _prompt(chat_id, message_id, bot, state, locale, back)


@router.message(LoginState.waiting_for_login, F.text)
async def login_text(message: Message, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    if message.from_user is None or message.chat.type != 'private':
        return
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    locale = normalize_locale(data.get('locale'))
    request = data.get('login_input') or {}
    account_id, message_id = request.get('account_id'), request.get('message_id')
    try:
        await _save(message.from_user.id, account_id, (message.text or '').strip(), backend)
    except BackendError as exc:
        back = await _back(locale, message.from_user.id, account_id)
        return await _prompt(message.chat.id, message_id, bot, state, locale, back, error_text(locale, exc))
    await state.set_state(None)
    await state.update_data(login_input=None)
    await _done(message.chat.id, message.from_user.id, message_id, account_id, bot, backend, state)
