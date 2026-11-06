"""Merging accounts in the bot. A member hands out a code from the account to
be merged in, or enters one in the main account, and sees the request; an
administrator sees both accounts and what the merge does, then approves
(choosing the VPN profile when both have one) or rejects."""
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from ..backend import BackendClient, BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen
from .common import render

router = Router()

ERRORS = {'merge_code_invalid', 'merge_same_account', 'merge_staff_account', 'merge_already_pending',
          'merge_profile_choice_required', 'merge_own_request', 'sign_in_rate_limited'}


class MergeState(StatesGroup):
    waiting_for_code = State()


async def _locale(state):
    return normalize_locale((await state.get_data()).get('locale'))


def error_text(locale, exc):
    from .admin_settings import _friendly_error
    return tr(locale, 'merge.error.' + exc.code) if exc.code in ERRORS else _friendly_error(locale, exc)


def accounts_row(locale):
    return [InlineKeyboardButton(text=tr(locale, 'merge.requests'), callback_data='amerge:list')]


def _name(locale, account):
    return account.get('login') or (f"@{account['username']}" if account.get('username') else None) \
        or str(account.get('telegram_user_id') or account.get('display_name') or account['id'][:8])


def describe(locale, preview):
    """Both accounts and what the merge does, as lines."""
    lines = []
    for key in ('main', 'other'):
        account = preview[key]
        vpn = account.get('vpn')
        lines.append(tr(locale, 'merge.account_' + key, name=_name(locale, account)))
        lines.append(tr(locale, 'merge.vpn', devices=vpn['devices'], tariff=vpn.get('tariff') or '—')
                     if vpn else tr(locale, 'merge.no_vpn'))
    result = preview['result']
    if result['vpn'] == 'moves':
        lines.append(tr(locale, 'merge.result.vpn_moves'))
    elif result['vpn'] == 'choose':
        lines.append(tr(locale, 'merge.result.vpn_choose'))
    for key in ('telegram', 'password', 'login'):
        if result.get(key):
            lines.append(tr(locale, f'merge.result.{key}_{result[key]}'))
    if result.get('passkeys_removed'):
        lines.append(tr(locale, 'merge.result.passkeys', count=result['passkeys_removed']))
    lines.append(tr(locale, 'merge.result.other_deleted'))
    return lines


async def show_member(chat_id, user_id, message_id, bot, backend, state, notice=None):
    locale = await _locale(state)
    items = (await backend.request('GET', '/api/v1/me/merge-requests', telegram_user_id=user_id))['items']
    pending = next((item for item in items if item['status'] == 'pending'), None)
    from .user import button
    lines = ((notice,) if notice else ()) + (tr(locale, 'merge.member_hint'),)
    rows = []
    if pending:
        lines += (tr(locale, 'merge.pending'), *describe(locale, pending['preview']))
        rows.append([InlineKeyboardButton(text=tr(locale, 'merge.cancel'),
                                          callback_data='merge:cancel:' + pending['id'], style='danger')])
    else:
        rows += [[InlineKeyboardButton(text=tr(locale, 'merge.give_code'), callback_data='merge:code')],
                 [InlineKeyboardButton(text=tr(locale, 'merge.enter_code'), callback_data='merge:enter')]]
    rows.append([button(user_id, tr(locale, 'back'), 'member_settings')])
    await render(bot, chat_id, Screen(tr(locale, 'merge.title'), lines, embedded_buttons=True, navigation=True),
                 rows, state, message_id)


@router.callback_query(F.data.startswith('merge:'))
async def member_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    locale = await _locale(state)
    chat_id, user_id, message_id = query.message.chat.id, query.from_user.id, query.message.message_id
    action = query.data.removeprefix('merge:')
    from .user import button
    try:
        if action == 'code':
            value = await backend.request('POST', '/api/v1/me/merge-code', telegram_user_id=user_id)
            return await render(bot, chat_id, Screen(tr(locale, 'merge.title'), (
                tr(locale, 'merge.code', code=value['code']), tr(locale, 'merge.code_hint')),
                embedded_buttons=True, navigation=True),
                [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data='merge:view')]], state, message_id)
        if action == 'enter':
            await state.set_state(MergeState.waiting_for_code)
            await state.update_data(merge_message_id=message_id)
            return await render(bot, chat_id, Screen(tr(locale, 'merge.title'), (tr(locale, 'merge.enter_hint'),),
                                embedded_buttons=True, navigation=True),
                                [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data='merge:view')]],
                                state, message_id)
        if action.startswith('cancel:'):
            await backend.request('DELETE', '/api/v1/me/merge-requests/' + action.removeprefix('cancel:'),
                                  telegram_user_id=user_id)
    except BackendError as exc:
        await state.set_state(None)
        return await show_member(chat_id, user_id, message_id, bot, backend, state, error_text(locale, exc))
    await state.set_state(None)
    await show_member(chat_id, user_id, message_id, bot, backend, state)


@router.message(MergeState.waiting_for_code, F.text)
async def code_text(message: Message, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    if message.from_user is None or message.chat.type != 'private':
        return
    try:
        await message.delete()
    except Exception:
        pass
    locale = await _locale(state)
    message_id = (await state.get_data()).get('merge_message_id')
    notice = None
    try:
        await backend.request('POST', '/api/v1/me/merge-requests', telegram_user_id=message.from_user.id,
                              body={'code': (message.text or '').strip()[:16]})
    except BackendError as exc:
        notice = error_text(locale, exc)
    await state.set_state(None)
    await show_member(message.chat.id, message.from_user.id, message_id, bot, backend, state, notice)


async def show_admin_list(query, bot, backend, state, notice=None):
    locale = await _locale(state)
    items = [item for item in (await backend.request('GET', '/api/v1/merge-requests',
                                                     telegram_user_id=query.from_user.id))['items']
             if item['status'] == 'pending']
    rows = [[InlineKeyboardButton(text=tr(locale, 'merge.item', main=_name(locale, item['preview']['main']),
                                          other=_name(locale, item['preview']['other'])),
                                  callback_data='amerge:' + item['id'])] for item in items[:20]]
    rows.append([InlineKeyboardButton(text=tr(locale, 'back'), callback_data='accounts')])
    lines = ((notice,) if notice else ()) + ((tr(locale, 'merge.choose'),) if items else (tr(locale, 'merge.empty'),))
    await render(bot, query.message.chat.id, Screen(tr(locale, 'merge.requests'), lines, embedded_buttons=True,
                 navigation=True), rows, state, query.message.message_id)


@router.callback_query(F.data.startswith('amerge:'))
async def admin_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    locale = await _locale(state)
    parts = query.data.split(':')
    if parts[1] == 'list':
        return await show_admin_list(query, bot, backend, state)
    try:
        if parts[1] in {'ok', 'no'} and len(parts) >= 3:
            body = {'decision': 'approve' if parts[1] == 'ok' else 'reject'}
            if len(parts) == 4:
                body['keep_profile'] = 'main' if parts[3] == 'm' else 'other'
            await backend.request('POST', f'/api/v1/merge-requests/{parts[2]}/decision',
                                  telegram_user_id=query.from_user.id, command=True, body=body)
            return await show_admin_list(query, bot, backend, state, tr(locale, 'merge.decided_' + parts[1]))
        item = await backend.request('GET', '/api/v1/merge-requests/' + parts[1], telegram_user_id=query.from_user.id)
    except BackendError as exc:
        return await show_admin_list(query, bot, backend, state, error_text(locale, exc))
    result = item['preview']['result']
    if result['vpn'] == 'choose':
        rows = [[InlineKeyboardButton(text=tr(locale, 'merge.keep_main'), callback_data=f"amerge:ok:{item['id']}:m",
                                      style='primary')]]
        if not result.get('keep_main_only'):
            rows.append([InlineKeyboardButton(text=tr(locale, 'merge.keep_other'),
                                              callback_data=f"amerge:ok:{item['id']}:o")])
    else:
        rows = [[InlineKeyboardButton(text=tr(locale, 'merge.approve'), callback_data='amerge:ok:' + item['id'],
                                      style='primary')]]
    rows += [[InlineKeyboardButton(text=tr(locale, 'merge.reject'), callback_data='amerge:no:' + item['id'],
                                   style='danger')],
             [InlineKeyboardButton(text=tr(locale, 'back'), callback_data='amerge:list')]]
    await render(bot, query.message.chat.id, Screen(tr(locale, 'merge.title'), tuple(describe(locale, item['preview'])),
                 embedded_buttons=True, navigation=True), rows, state, query.message.message_id)
