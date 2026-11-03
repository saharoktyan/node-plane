"""Renewal requests: administrators extend or decline a member's access.

A request is approved for the profile's tariff term or for chosen days
(backend.renewals). The notice sent to administrators and the list under
Requests share one decision screen.
"""
import logging
import os

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from ..backend import BackendClient, BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen, Section, Table
from .common import render, send_notice

router = Router()

# Offered when the profile has no tariff term.
DAYS = (7, 30, 90, 180, 365)
PAGE = 20


def button(text, data, *, style=None):
    return InlineKeyboardButton(text=text, callback_data=data, style=style)


def _lines(locale, item):
    expiry = item.get('profile_expires_at')
    tariff = item.get('tariff')
    return Table((tr(locale, 'maintenance.rich.field'), tr(locale, 'maintenance.rich.value')), (
        (tr(locale, 'renewal.profile'), item['display_name']),
        (tr(locale, 'account.rich.expiry'), expiry[:10] + ' UTC' if expiry else tr(locale, 'profile.layout.unlimited')),
        (tr(locale, 'tariff.profile'), tariff['title'] if tariff else tr(locale, 'tariff.profile.none')),
        (tr(locale, 'renewal.requested'), item['created_at'][:16].replace('T', ' ') + ' UTC')))


def decision_rows(locale, item):
    """Approve for the tariff term or chosen days, or decline."""
    term = (item.get('tariff') or {}).get('duration_days')
    days = (term,) if term else DAYS
    approve = [button(tr(locale, 'renewal.approve_days', days=value), f"rnw_ok:{item['id']}:{value}",
                      style='primary') for value in days]
    return [*(approve[i:i + 3] for i in range(0, len(approve), 3)),
            [button(tr(locale, 'renewal.reject'), f"rnw_no:{item['id']}", style='danger')]]


async def show_list(query, bot, backend, state, offset=0):
    locale = normalize_locale((await state.get_data()).get('locale'))
    page = await backend.renewal_requests(query.from_user.id, offset)
    rows = [[button(f"{item['display_name']} · {(item.get('profile_expires_at') or '')[:10]}", f"rnw:{item['id']}")]
            for item in page['items']]
    navigation = []
    if offset:
        navigation.append(button('←', f'rnw_list:{max(0, offset - PAGE)}'))
    if offset + len(page['items']) < page['total']:
        navigation.append(button('→', f'rnw_list:{offset + PAGE}'))
    if navigation:
        rows.append(navigation)
    from .callbacks import RequestsCallback
    rows.append([button(tr(locale, 'back'), RequestsCallback().pack())])
    await render(bot, query.message.chat.id, Screen(tr(locale, 'renewal.title'),
        (tr(locale, 'renewal.choose') if page['items'] else tr(locale, 'renewal.empty'),),
        embedded_buttons=True, navigation=True), rows, state, query.message.message_id)


async def entry_rows(backend, user_id, locale):
    """The Requests screen's entry, shown while renewals wait."""
    if not hasattr(backend, 'renewal_requests'):
        return []
    try:
        page = await backend.renewal_requests(user_id)
    except BackendError:
        return []
    if not page['total']:
        return []
    return [[button(tr(locale, 'renewal.entry', count=page['total']), 'rnw_list:0', style='primary')]]


@router.callback_query(F.data.startswith('rnw_list:'))
async def list_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    raw = query.data.split(':', 1)[1]
    await show_list(query, bot, backend, state, int(raw) if raw.isdigit() else 0)


@router.callback_query(F.data.startswith('rnw:'))
async def request_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    locale = normalize_locale((await state.get_data()).get('locale'))
    try:
        item = await backend.request('GET', f"/api/v1/renewal-requests/{query.data.split(':', 1)[1]}",
                                     telegram_user_id=query.from_user.id)
    except BackendError:
        return await show_list(query, bot, backend, state)
    rows = decision_rows(locale, item) if item['status'] == 'pending' else []
    rows.append([button(tr(locale, 'back'), 'rnw_list:0')])
    lines = () if item['status'] == 'pending' else (tr(locale, 'renewal.status.' + item['status']),)
    await render(bot, query.message.chat.id, Screen(tr(locale, 'renewal.title'), lines,
        sections=(Section('', tables=(_lines(locale, item),)),), embedded_buttons=True, navigation=True), rows, state, query.message.message_id)


@router.callback_query(F.data.startswith('rnw_ok:') | F.data.startswith('rnw_no:'))
async def decide_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    locale = normalize_locale((await state.get_data()).get('locale'))
    parts = query.data.split(':')
    request_id, user_id = parts[1], query.from_user.id
    try:
        item = await backend.request('GET', f'/api/v1/renewal-requests/{request_id}', telegram_user_id=user_id)
        if item['status'] == 'pending':
            if parts[0] == 'rnw_no':
                await backend.request('POST', f'/api/v1/renewal-requests/{request_id}/reject', telegram_user_id=user_id)
            else:
                days = int(parts[2]) if len(parts) == 3 and parts[2].isdigit() else 0
                term = (item.get('tariff') or {}).get('duration_days')
                if days not in (term,) + DAYS:
                    return
                await backend.request('POST', f'/api/v1/renewal-requests/{request_id}/approve',
                    telegram_user_id=user_id, body={'days': days}, command=True,
                    revision=item['profile_revision'])
            item = await backend.request('GET', f'/api/v1/renewal-requests/{request_id}', telegram_user_id=user_id)
        note = tr(locale, 'renewal.status.' + item['status'], date=(item.get('expires_at') or '')[:10])
    except BackendError as exc:
        note = tr(locale, 'renewal.failed' if exc.code != 'resource_not_found' else 'renewal.missing')
    await render(bot, query.message.chat.id, Screen(tr(locale, 'renewal.title'), (note,),
        embedded_buttons=True, navigation=True), [[button(tr(locale, 'renewal.list'), 'rnw_list:0')]],
        state, query.message.message_id)


async def notify_admins(bot: Bot, backend: BackendClient, request_id: str):
    """Tell administrators about a new request, as for access requests."""
    try:
        admin_ids = (await backend.request('GET', '/api/v1/integrations/telegram/access-decisions/recipients'))['items']
    except BackendError as error:
        if error.status != 404:
            logging.getLogger(__name__).warning('Renewal notification recipients unavailable')
            return
        admin_ids = os.environ.get('ADMIN_IDS', '').split(',')
    for raw_id in admin_ids:
        try:
            admin_id = int(str(raw_id).strip())
            admin = await backend.me(admin_id)
            if admin['role'] != 'admin' or admin['status'] != 'approved':
                continue
            if not (await backend.access_request_policy(admin_id)).get('notify_requests', True):
                continue
            locale = normalize_locale(admin.get('locale') or admin.get('language_code'))
            item = await backend.request('GET', f'/api/v1/renewal-requests/{request_id}', telegram_user_id=admin_id)
            notice = Screen(tr(locale, 'renewal.title'), (tr(locale, 'renewal.notification'),),
                            sections=(Section('', tables=(_lines(locale, item),)),), embedded_buttons=True)
            markup = InlineKeyboardMarkup(inline_keyboard=[
                *decision_rows(locale, item), [button(tr(locale, 'setup.close'), 'notification_close')]])
            await send_notice(bot, admin_id, notice, markup)
        except (ValueError, BackendError, TelegramAPIError):
            continue
