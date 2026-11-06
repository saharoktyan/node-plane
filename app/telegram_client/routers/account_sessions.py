"""Passwords, passkeys and sessions in the bot. Neither a password nor a
passkey is added here, since sign-in methods do not come through Telegram:
the member sees what is set, lists their sessions and passkeys and ends or
removes them; an administrator sees the same on an account's card, signs the
account out everywhere, removes its passkeys and unlinks the Workstations
it linked through the relay."""
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
    """The member's password and passkeys in Settings, with their sessions."""
    lines = (tr(locale, 'sessions.password_set' if current.get('has_password') else 'sessions.no_password'),
             tr(locale, 'sessions.passkeys', count=current.get('passkeys', 0)))
    rows = ((InlineKeyboardButton(text=tr(locale, 'sessions.open'), callback_data='msess:list'),),) \
        if current.get('has_password') or current.get('passkeys') else ()
    # Moving this Telegram to an account of the web cabinet.
    rows += ((InlineKeyboardButton(text=tr(locale, 'sessions.telegram_link'), callback_data='msess:tglink'),),
             (InlineKeyboardButton(text=tr(locale, 'merge.title'), callback_data='merge:view'),))
    return Section(tr(locale, 'sessions.title'), lines, rows=rows)


def card_lines(locale, account):
    return (tr(locale, 'sessions.card_password' if account.get('has_password') else 'sessions.card_no_password'),
            tr(locale, 'sessions.card_count', count=account.get('sessions', 0)),
            tr(locale, 'sessions.card_passkeys', count=account.get('passkeys', 0))) + (
        (tr(locale, 'sessions.card_workstations', count=account['workstations']),)
        if account.get('workstations') else ())


def card_rows(locale, account):
    rows = []
    if account.get('sessions'):
        rows.append([InlineKeyboardButton(text=tr(locale, 'sessions.end_account'),
                                          callback_data='asess:' + account['id'])])
    if account.get('passkeys'):
        rows.append([InlineKeyboardButton(text=tr(locale, 'sessions.remove_passkeys'),
                                          callback_data='akeys:' + account['id'])])
    if account.get('workstations'):
        rows.append([InlineKeyboardButton(text=tr(locale, 'sessions.unlink_workstations'),
                                          callback_data='awks:' + account['id'])])
    return rows


async def show_sessions(chat_id, user_id, message_id, bot, backend, state, notice=None):
    locale = normalize_locale((await state.get_data()).get('locale'))
    items = (await backend.request('GET', '/api/v1/me/sessions', telegram_user_id=user_id))['items']
    keys = (await backend.request('GET', '/api/v1/me/passkeys', telegram_user_id=user_id))['items']
    lines = ((notice,) if notice else ()) + (tuple(
        tr(locale, 'sessions.item', device=item['device'], used=_date(item['used_at'])) for item in items
    ) or (tr(locale, 'sessions.empty'),))
    if keys:
        lines += (tr(locale, 'sessions.passkeys_title'),) + tuple(
            tr(locale, 'sessions.passkey', name=key['name'], created=_date(key['created_at'])) for key in keys)
    from .user import button
    rows = [[InlineKeyboardButton(text=tr(locale, 'sessions.end', device=item['device'][:40]),
                                  callback_data='msess:end:' + item['id'])] for item in items]
    if len(items) > 1:
        rows.append([InlineKeyboardButton(text=tr(locale, 'sessions.end_all'), callback_data='msess:all')])
    # Callback data fits 64 bytes, so a passkey is named by its place in the list.
    rows += [[InlineKeyboardButton(text=tr(locale, 'sessions.remove_passkey', name=key['name'][:40]),
                                   callback_data=f'msess:key:{index}')] for index, key in enumerate(keys)]
    await state.update_data(passkey_ids=[key['id'] for key in keys])
    rows.append([button(user_id, tr(locale, 'back'), 'member_settings')])
    await render(bot, chat_id, Screen(tr(locale, 'sessions.title'), lines, embedded_buttons=True, navigation=True),
                 rows, state, message_id)


async def show_link_code(chat_id, user_id, message_id, bot, backend, state):
    """A code to enter in the web cabinet, which takes this Telegram over."""
    locale = normalize_locale((await state.get_data()).get('locale'))
    try:
        code = await backend.request('POST', '/api/v1/me/telegram/link-code', telegram_user_id=user_id)
        lines = (tr(locale, 'sessions.telegram_code', code=code['code']), tr(locale, 'sessions.telegram_code_hint'))
    except BackendError as exc:
        if exc.code == 'telegram_account_has_access':
            lines = (tr(locale, 'sessions.telegram_has_access'),)
        else:
            from .admin_settings import _friendly_error
            lines = (_friendly_error(locale, exc),)
    from .user import button
    await render(bot, chat_id, Screen(tr(locale, 'sessions.telegram_link'), lines, embedded_buttons=True,
                 navigation=True), [[button(user_id, tr(locale, 'back'), 'member_settings')]], state, message_id)


@router.callback_query(F.data.startswith('msess:'))
async def sessions_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    chat_id, user_id, message_id = query.message.chat.id, query.from_user.id, query.message.message_id
    action = query.data.removeprefix('msess:')
    if action == 'tglink':
        return await show_link_code(chat_id, user_id, message_id, bot, backend, state)
    notice = None
    try:
        if action == 'all':
            # The bot is no session of its own, so this ends all of them.
            await backend.request('DELETE', '/api/v1/me/sessions', telegram_user_id=user_id)
        elif action.startswith('end:'):
            await backend.request('DELETE', '/api/v1/me/sessions/' + action.removeprefix('end:'),
                                  telegram_user_id=user_id)
        elif action.startswith('key:'):
            ids = (await state.get_data()).get('passkey_ids') or []
            index = int(action.removeprefix('key:')) if action.removeprefix('key:').isdigit() else len(ids)
            if index < len(ids):
                await backend.request('DELETE', '/api/v1/me/passkeys/' + ids[index], telegram_user_id=user_id)
    except BackendError as exc:
        from .admin_settings import _friendly_error
        notice = _friendly_error(normalize_locale((await state.get_data()).get('locale')), exc)
    await show_sessions(chat_id, user_id, message_id, bot, backend, state, notice)


@router.callback_query(F.data.startswith(('asess:', 'akeys:', 'akeysok:', 'awks:', 'awksok:')))
async def account_sessions_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    kind, account_id = query.data.split(':', 1)
    if kind in {'akeys', 'awks'}:
        # Removing passkeys may leave the account without a way into the cabinet,
        # and unlinking stops its Workstations: ask first.
        locale = normalize_locale((await state.get_data()).get('locale'))
        title, question = (('sessions.remove_passkeys', 'sessions.remove_passkeys_confirm') if kind == 'akeys'
                           else ('sessions.unlink_workstations', 'sessions.unlink_workstations_confirm'))
        return await render(bot, query.message.chat.id, Screen(tr(locale, title), (tr(locale, question),),
            embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'sessions.confirm_remove'), callback_data=kind + 'ok:' + account_id,
                                   style='danger')],
             [InlineKeyboardButton(text=tr(locale, 'back'), callback_data='account:' + account_id)]],
            state, query.message.message_id)
    what = {'asess': 'sessions', 'akeysok': 'passkeys', 'awksok': 'workstations'}[kind]
    await backend.request('DELETE', f'/api/v1/accounts/{account_id}/{what}',
                          telegram_user_id=query.from_user.id, command=True)
    from .admin_profiles import show_account
    await show_account(query.message.chat.id, query.from_user.id, query.message.message_id, account_id,
                       bot, backend, state)
