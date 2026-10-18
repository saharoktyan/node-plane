"""Resumable administrator guide using existing settings and setup screens."""
from aiogram import F, Router
from aiogram.types import InlineKeyboardButton

from ..i18n import normalize_locale, tr
from ..screens import Screen, Section, Breadcrumb
from .common import render, schedule_refresh
from .callbacks import AdminNodeCallback

router = Router()


async def show(chat_id, user_id, message_id, bot, backend, state):
    data = await state.get_data()
    reviewed = bool(data.get('quick_start_defaults_reviewed') or
                    data.get('installation_defaults_return') == 'quick_start')
    await state.set_state(None)
    await state.update_data(quick_start_dismiss_confirmation=None,
        quick_start_defaults_reviewed=reviewed, installation_defaults_return=None)
    locale = normalize_locale(data.get('locale'))
    value = await backend.request('GET', '/api/v1/system/onboarding', telegram_user_id=user_id)
    from .user import show_home, button as member_button
    if value.get('completed'):
        await show_home(chat_id, user_id, bot, backend, state, message_id)
        return
    await state.update_data(quick_start_home=member_button(user_id, tr(locale, 'navigation.menu'), 'home').callback_data)
    lines = tuple(('✓ ' if step['complete'] else '○ ') + tr(locale, 'quick_start.' + step['key'])
                  for step in value['steps'])
    step = value['next_step']
    callback = {'defaults': 'quick_start_defaults' if reviewed else 'idefault:guide',
        'node': 'quick_start_open:node', 'protocols': 'quick_start_open:protocols',
        'access': 'quick_start_open:access', 'config': 'quick_start_open:config'}.get(step)
    rows = []
    if callback and len(callback.encode()) > 64:
        callback = 'admin_nodes'
    if callback:
        rows.append([InlineKeyboardButton(text=tr(locale, 'quick_start.continue'), callback_data=callback, style='primary')])
    if step == 'defaults':
        rows.append([InlineKeyboardButton(text=tr(locale, 'quick_start.edit_defaults' if reviewed else 'quick_start.use_defaults'),
            callback_data='idefault:guide' if reviewed else 'quick_start_defaults')])
    if step is None:
        rows.append([InlineKeyboardButton(text=tr(locale, 'quick_start.done'), callback_data='quick_start_done', style='primary')])
    else:
        rows.append([InlineKeyboardButton(text=tr(locale, 'quick_start.dismiss'), callback_data='quick_start_dismiss')])
    if step is not None:
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


@router.callback_query(F.data.startswith('quick_start_open:'))
async def open_step(query, bot, backend, state):
    step = query.data.split(':', 1)[1]
    if step not in {'node', 'protocols', 'access', 'config'}:
        await query.answer()
        return
    progress = await backend.request('GET', '/api/v1/system/onboarding',
                                     telegram_user_id=query.from_user.id)
    if progress.get('next_step') != step:
        await query.answer()
        await show(query.message.chat.id, query.from_user.id, query.message.message_id,
                   bot, backend, state)
        return
    await state.update_data(quick_start_context={'step': step})
    if step == 'node':
        from .admin_nodes import new_node_cb
        await new_node_cb(query, bot, backend, state)
    elif step == 'protocols':
        from .admin_nodes import admin_node_cb
        node_key = progress.get('node_key')
        if not node_key:
            await query.answer()
            await state.update_data(quick_start_context={'step': 'node'})
            from .admin_nodes import show_admin_nodes
            await show_admin_nodes(query.message.chat.id, query.from_user.id,
                query.message.message_id, bot, backend, state)
            return
        await state.update_data(node_return={'node_key': node_key, 'callback': 'quick_start'})
        await admin_node_cb(query, AdminNodeCallback(node_key=node_key), bot, backend, state)
    elif step == 'access':
        await query.answer()
        from .admin_profiles import show_admin_profiles
        await state.set_state(None)
        await show_admin_profiles(query.message.chat.id, query.from_user.id,
            query.message.message_id, bot, backend, state, page_index=0)
    else:
        await query.answer()
        from .user import show_profiles
        await state.update_data(member_profile_back='quick_start')
        await show_profiles(query.message.chat.id, query.from_user.id,
            query.message.message_id, bot, backend, state)


@router.callback_query(F.data == 'quick_start_defaults')
async def use_defaults(query, bot, backend, state):
    await query.answer()
    await backend.request('POST', '/api/v1/system/onboarding/defaults', telegram_user_id=query.from_user.id, body={})
    await show(query.message.chat.id, query.from_user.id, query.message.message_id, bot, backend, state)


@router.callback_query(F.data == 'quick_start_done')
async def done(query, bot, backend, state):
    await query.answer()
    await backend.request('POST', '/api/v1/system/onboarding/complete/telegram', telegram_user_id=query.from_user.id, body={})
    await state.update_data(quick_start_context=None, node_return=None, member_profile_back='profiles')
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
    await state.update_data(quick_start_dismiss_confirmation=None, quick_start_context=None, node_return=None,
        member_profile_back='profiles')
    await backend.request('POST', '/api/v1/system/onboarding/dismiss/telegram', telegram_user_id=query.from_user.id, body={})
    from .user import show_home
    await show_home(query.message.chat.id, query.from_user.id, bot, backend, state, query.message.message_id)
