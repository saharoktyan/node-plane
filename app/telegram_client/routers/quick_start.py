"""Resumable administrator guide using existing settings and setup screens."""
from aiogram import F, Router
from aiogram.types import InlineKeyboardButton

from ..i18n import normalize_locale, tr
from ..screens import Screen, Section, Breadcrumb
from .common import render, schedule_refresh
from .callbacks import NewNodeCallback, AdminNodeCallback

router = Router()


async def show(chat_id, user_id, message_id, bot, backend, state):
    await state.update_data(quick_start_dismiss_confirmation=None)
    locale = normalize_locale((await state.get_data()).get('locale'))
    value = await backend.request('GET', '/api/v1/system/onboarding', telegram_user_id=user_id)
    from .user import show_home, button as member_button
    if value.get('completed'):
        await show_home(chat_id, user_id, bot, backend, state, message_id)
        return
    await state.update_data(quick_start_home=member_button(user_id, tr(locale, 'navigation.menu'), 'home').callback_data)
    lines = tuple(('✓ ' if step['complete'] else '○ ') + tr(locale, 'quick_start.' + step['key'])
                  for step in value['steps'])
    step = value['next_step']
    callback = {'defaults': 'idefault:open', 'node': NewNodeCallback().pack(),
        'protocols': AdminNodeCallback(node_key=value['node_key']).pack() if value['node_key'] else 'admin_nodes',
        'access': 'admin_profiles', 'config': member_button(user_id, tr(locale, 'quick_start.continue'), 'profiles').callback_data}.get(step)
    rows = []
    if callback and len(callback.encode()) > 64:
        callback = 'admin_nodes'
    if callback:
        rows.append([InlineKeyboardButton(text=tr(locale, 'quick_start.continue'), callback_data=callback, style='primary')])
    if step == 'defaults':
        rows.append([InlineKeyboardButton(text=tr(locale, 'quick_start.use_defaults'), callback_data='quick_start_defaults')])
    if step is None:
        rows.append([InlineKeyboardButton(text=tr(locale, 'quick_start.done'), callback_data='quick_start_done', style='primary')])
    else:
        rows.append([InlineKeyboardButton(text=tr(locale, 'quick_start.dismiss'), callback_data='quick_start_dismiss')])
    rows.append([member_button(user_id, tr(locale, 'back'), 'home')])
    await render(bot, chat_id, Screen(tr(locale, 'quick_start.title'),
        (tr(locale, 'quick_start.note.' + (step or 'done')),), sections=(
            Section(tr(locale, 'quick_start.progress'), lines=lines),), embedded_buttons=True, navigation=True, breadcrumbs=(
                Breadcrumb(tr(locale, 'navigation.menu'), member_button(user_id, tr(locale, 'navigation.menu'), 'home').callback_data),
                Breadcrumb(tr(locale, 'quick_start.title'), 'quick_start'))),
        rows, state, message_id)
    await state.update_data(admin_panel_active=True)
    if step is not None:
        async def refresh():
            await show(chat_id, user_id, message_id, bot, backend, state)
        schedule_refresh(state, refresh)


@router.callback_query(F.data == 'quick_start')
async def guide(query, bot, backend, state):
    await query.answer()
    await show(query.message.chat.id, query.from_user.id, query.message.message_id, bot, backend, state)


@router.callback_query(F.data == 'quick_start_defaults')
async def use_defaults(query, bot, backend, state):
    await query.answer()
    await backend.request('POST', '/api/v1/system/onboarding/defaults', telegram_user_id=query.from_user.id, body={})
    await show(query.message.chat.id, query.from_user.id, query.message.message_id, bot, backend, state)


@router.callback_query(F.data == 'quick_start_done')
async def done(query, bot, backend, state):
    await query.answer()
    await backend.request('POST', '/api/v1/system/onboarding/complete/telegram', telegram_user_id=query.from_user.id, body={})
    from .user import show_home
    await show_home(query.message.chat.id, query.from_user.id, bot, backend, state, query.message.message_id)


@router.callback_query(F.data == 'quick_start_dismiss')
async def dismiss_confirmation(query, bot, state):
    from uuid import uuid4
    await query.answer()
    locale = normalize_locale((await state.get_data()).get('locale'))
    nonce = uuid4().hex[:16]
    await state.update_data(quick_start_dismiss_confirmation={'owner': query.from_user.id, 'nonce': nonce})
    await render(bot, query.message.chat.id, Screen(tr(locale, 'quick_start.dismiss'),
        (tr(locale, 'quick_start.dismiss_note'),), embedded_buttons=True, navigation=True),
        [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data='quick_start'),
          InlineKeyboardButton(text=tr(locale, 'policy.confirm'), callback_data='quick_start_dismiss_confirm:' + nonce)]],
        state, query.message.message_id)


@router.callback_query(F.data.startswith('quick_start_dismiss_confirm:'))
async def dismiss_confirmed(query, bot, backend, state):
    await query.answer()
    pending = (await state.get_data()).get('quick_start_dismiss_confirmation') or {}
    if pending.get('owner') != query.from_user.id or pending.get('nonce') != query.data.split(':', 1)[1]:
        return
    await state.update_data(quick_start_dismiss_confirmation=None)
    await backend.request('POST', '/api/v1/system/onboarding/dismiss/telegram', telegram_user_id=query.from_user.id, body={})
    from .user import show_home
    await show_home(query.message.chat.id, query.from_user.id, bot, backend, state, query.message.message_id)
