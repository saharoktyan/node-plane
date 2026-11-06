"""Invites in the bot. An administrator invites someone new from Accounts: the
link `t.me/<bot>?start=inv_…` registers the person with the invite's template
(nothing, VPN, or VPN with a tariff). A person opening such a link gets an
account with their Telegram. Invites for an existing account sign in as that
account, so they are made only in Workstation (or the web cabinet)."""
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from ..backend import BackendClient, BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen
from .common import render

router = Router()

ERRORS = {'invite_invalid', 'invite_already_registered', 'invite_not_for_telegram', 'invite_creator_unavailable',
          'invite_limit', 'sign_in_rate_limited'}


def _date(value):
    return datetime.fromisoformat(value).strftime('%Y-%m-%d')


def error_text(locale, exc):
    from .admin_settings import _friendly_error
    return tr(locale, 'invite.error.' + exc.code) if exc.code in ERRORS else _friendly_error(locale, exc)


def accounts_row(locale):
    return [InlineKeyboardButton(text=tr(locale, 'invite.create'), callback_data='inv:new'),
            InlineKeyboardButton(text=tr(locale, 'invite.list'), callback_data='inv:list')]


async def _locale(state):
    return normalize_locale((await state.get_data()).get('locale'))


def _back(locale, callback='accounts'):
    return [InlineKeyboardButton(text=tr(locale, 'back'), callback_data=callback)]


async def _link(bot, token):
    me = await bot.me()
    return f'https://t.me/{me.username}?start={token}'


async def _templates(query, bot, backend, state, locale):
    tariffs = (await backend.request('GET', '/api/v1/tariffs', telegram_user_id=query.from_user.id)).get('items', [])
    rows = [[InlineKeyboardButton(text=tr(locale, 'invite.template.none'), callback_data='inv:make:none')],
            [InlineKeyboardButton(text=tr(locale, 'invite.template.vpn'), callback_data='inv:make:vpn')]]
    rows += [[InlineKeyboardButton(text=tr(locale, 'invite.template.tariff', title=tariff['title'][:40]),
                                   callback_data='inv:make:t:' + tariff['id'])] for tariff in tariffs[:20]]
    rows.append(_back(locale))
    await render(bot, query.message.chat.id, Screen(tr(locale, 'invite.title'), (tr(locale, 'invite.choose'),),
                 embedded_buttons=True, navigation=True), rows, state, query.message.message_id)


async def _made(query, bot, state, locale, invite):
    lines = [tr(locale, 'invite.ready', date=_date(invite['expires_at'])), await _link(bot, invite['token'])]
    if invite.get('cabinet_link'):
        lines += [tr(locale, 'invite.cabinet'), invite['cabinet_link']]
    await render(bot, query.message.chat.id, Screen(tr(locale, 'invite.title'), tuple(lines),
                 embedded_buttons=True, navigation=True),
                 [[InlineKeyboardButton(text=tr(locale, 'invite.revoke'), callback_data='inv:revoke:' + invite['id'],
                                        style='danger')], _back(locale, 'inv:list')],
                 state, query.message.message_id)


async def _listed(query, bot, backend, state, locale, notice=None):
    items = [item for item in (await backend.request('GET', '/api/v1/invites',
                                                     telegram_user_id=query.from_user.id))['items']
             if item['status'] == 'active']
    lines = ((notice,) if notice else ()) + (tuple(
        tr(locale, 'invite.item', template=_template(locale, item), date=_date(item['expires_at'])) for item in items
    ) or (tr(locale, 'invite.empty'),))
    rows = [[InlineKeyboardButton(text=tr(locale, 'invite.revoke_item', index=index + 1),
                                  callback_data='inv:revoke:' + item['id'])] for index, item in enumerate(items[:20])]
    rows.append(_back(locale))
    await render(bot, query.message.chat.id, Screen(tr(locale, 'invite.list'), lines, embedded_buttons=True,
                 navigation=True), rows, state, query.message.message_id)


def _template(locale, item):
    if item['kind'] == 'account':
        return tr(locale, 'invite.template.account')
    if not item['vpn']:
        return tr(locale, 'invite.template.none')
    return tr(locale, 'invite.template.vpn_tariff' if item['tariff_id'] else 'invite.template.vpn')


@router.callback_query(F.data.startswith('inv:'))
async def invites_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    locale = await _locale(state)
    action = query.data.removeprefix('inv:')
    try:
        if action == 'new':
            return await _templates(query, bot, backend, state, locale)
        if action == 'list':
            return await _listed(query, bot, backend, state, locale)
        if action.startswith('make:'):
            template = action.removeprefix('make:')
            body = {'vpn': template != 'none'}
            if template.startswith('t:'):
                body['tariff_id'] = template.removeprefix('t:')
            invite = await backend.request('POST', '/api/v1/invites', telegram_user_id=query.from_user.id,
                                           command=True, body=body)
            return await _made(query, bot, state, locale, invite)
        if action.startswith('revoke:'):
            await backend.request('DELETE', '/api/v1/invites/' + action.removeprefix('revoke:'),
                                  telegram_user_id=query.from_user.id, command=True)
            return await _listed(query, bot, backend, state, locale, tr(locale, 'invite.revoked'))
    except BackendError as exc:
        await render(bot, query.message.chat.id, Screen(tr(locale, 'invite.title'), (error_text(locale, exc),),
                     embedded_buttons=True, navigation=True), [_back(locale)], state, query.message.message_id)


def token_of(message: Message):
    """The invite token of `/start inv_…`, if any."""
    parts = (getattr(message, 'text', None) or '').split(maxsplit=1)
    token = parts[1].strip() if len(parts) == 2 else ''
    return token if token.startswith('inv_') and len(token) <= 64 else None


async def redeem(message: Message, bot: Bot, backend: BackendClient, state: FSMContext, token: str) -> None:
    """Registers the person by the invite and shows what happened."""
    user_id = message.from_user.id
    locale = normalize_locale((await state.get_data()).get('locale') or message.from_user.language_code)
    from .user import button
    try:
        await backend.request('POST', '/api/v1/invites/accept/telegram', telegram_user_id=user_id,
                              body={'token': token})
    except BackendError as exc:
        return await render(bot, message.chat.id, Screen(tr(locale, 'invite.welcome_title'), (error_text(locale, exc),),
                            embedded_buttons=True, navigation=True),
                            [[button(user_id, tr(locale, 'invite.continue'), 'home')]], state)
    current = await backend.me(user_id)
    rows = []
    if not current.get('login'):
        from .account_login import settings_section
        rows += [list(row) for row in settings_section(locale, user_id, current).rows]
    rows.append([button(user_id, tr(locale, 'invite.continue'), 'home')])
    await render(bot, message.chat.id, Screen(tr(locale, 'invite.welcome_title'), (tr(locale, 'invite.welcome'),),
                 embedded_buttons=True, navigation=True), rows, state)
