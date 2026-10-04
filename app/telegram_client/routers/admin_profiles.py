"""Profile administration backed by the standalone backend API."""
from __future__ import annotations

import asyncio
import secrets
import logging
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from ..backend import BackendClient, BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen, Section, Table, server_label
from .callbacks import (AccountsCallback, AccountCallback, NewProfileCallback,
    AdminProfilesCallback, AdminProfileCallback, GrantNodesCallback,
    GrantProtocolsCallback, AddGrantCallback, RemoveGrantCallback,
    ToggleFreezeCallback)
from .common import render
from .states import ProfileDraftState

router = Router()


async def _all_nodes(backend, user_id):
    nodes, cursor, seen = [], None, set()
    while True:
        page = await backend.admin_nodes(user_id, cursor=cursor)
        nodes.extend(page['items'])
        cursor = page.get('next_cursor')
        if not cursor:
            return nodes
        if cursor in seen:
            raise BackendError('backend_unavailable', 503)
        seen.add(cursor)


async def _ensure_grant_draft(backend, user_id, profile_id, state):
    data = await state.get_data()
    if data.get('edit_profile_id') == profile_id:
        return data
    page, profile = await asyncio.gather(backend.profile_grants(user_id, profile_id),
        backend.request('GET', f'/api/v1/profiles/{profile_id}', telegram_user_id=user_id))
    await state.update_data(edit_profile_id=profile_id,
        draft_grants=[dict(item) for item in page['items']],
        original_grants=[dict(item) for item in page['items']],
        edit_profile_revision=profile['desired_revision'], grant_nodes_page=0)
    return await state.get_data()


def _status(profile, locale):
    if profile.get('deleting'):
        key = 'profile.admin.deleting'
    elif profile['frozen']:
        key = 'profile.frozen'
    elif profile.get('expires_at') and datetime.fromisoformat(profile['expires_at']) <= datetime.now(timezone.utc):
        key = 'profile.expired'
    else:
        key = 'profile.active'
    return tr(locale, key)


def _status_buttons(profile, locale):
    return tuple(InlineKeyboardButton(text=tr(locale, 'profile.frozen' if frozen else 'profile.active'),
        callback_data=f"prof_state:{profile['id']}:{'frozen' if frozen else 'active'}",
        style='primary' if profile['frozen'] == frozen else None) for frozen in (False, True))


def _task_status(status, locale):
    key = {'pending': 'awaiting_executor', 'dispatched': 'running', 'running': 'running',
        'succeeded': 'succeeded', 'blocked': 'blocked', 'superseded': 'superseded'}.get(status)
    return tr(locale, 'operation.' + key) if key else tr(locale, 'profile.rich.unknown')


def _bulk_grants(nodes, grants, region, add):
    scoped = [node for node in nodes if region is None or (node.get('region') or '') == region]
    if not add:
        keys = {node['key'] for node in scoped}
        return [] if region is None else [grant for grant in grants if grant['node_key'] not in keys]
    updated = [dict(grant) for grant in grants]
    seen = {(grant['node_key'], grant['protocol']) for grant in updated}
    for node in scoped:
        if not node.get('enabled', True):
            continue
        for protocol in node['protocols']:
            if (node['key'], protocol) not in seen:
                updated.append({'node_key': node['key'], 'protocol': protocol})
                seen.add((node['key'], protocol))
    return updated


def _bulk_buttons(profile_id, scope, locale, *, region=False, prefix='grant_bulk'):
    return (InlineKeyboardButton(text=tr(locale, 'profile.rich.grant_region' if region else 'profile.rich.grant_all'),
                callback_data=f'{prefix}:{profile_id}:add:{scope}'),
            InlineKeyboardButton(text=tr(locale, 'profile.rich.revoke_region' if region else 'profile.rich.revoke_all'),
                callback_data=f'{prefix}:{profile_id}:del:{scope}', style='danger'))


async def _locale(state: FSMContext) -> str:
    return normalize_locale((await state.get_data()).get('locale'))


async def _clear_flow(state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    if data.get('locale'):
        await state.update_data(locale=data['locale'])


def _profile_error(locale: str, error: BackendError) -> str:
    if error.status == 412:
        return tr(locale, 'profile.admin.error_changed')
    if error.status == 503:
        return tr(locale, 'profile.admin.error_unavailable')
    if error.code == 'grant_target_unavailable':
        return tr(locale, 'profile.admin.error_node')
    return tr(locale, 'profile.admin.error_generic')


class ProfileSearchState(StatesGroup):
    waiting_for_query = State()


class ProfileRenameState(StatesGroup):
    waiting_for_name = State()

class ProfileUserState(StatesGroup):
    waiting_for_telegram_id = State()

@router.callback_query(F.data == 'profile_add_user')
async def add_profile_user_cb(query: CallbackQuery, bot: Bot, state: FSMContext):
    await query.answer()
    await _clear_flow(state)
    await state.set_state(ProfileUserState.waiting_for_telegram_id)
    await render(bot, query.message.chat.id,
        Screen(tr(await _locale(state), 'profile.create.title'),
               (tr(await _locale(state), 'profile.create.telegram_prompt'),), embedded_buttons=True, navigation=True),
        [[InlineKeyboardButton(text=tr(await _locale(state), 'back'),
                              callback_data=AdminProfilesCallback().pack())]], state, query.message.message_id)

@router.message(ProfileUserState.waiting_for_telegram_id, F.text)
async def add_profile_user_text(message: Message, bot: Bot, backend: BackendClient, state: FSMContext):
    if message.from_user is None or message.chat.type != 'private':
        return
    data = await state.get_data()
    locale = await _locale(state)
    value = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass
    if not value.isascii() or not value.isdecimal() or not 0 < int(value) < 2**63:
        await render(bot, message.chat.id, Screen(tr(locale, 'profile.create.title'),
            (tr(locale, 'profile.create.telegram_prompt'),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=AdminProfilesCallback().pack())]],
            state, data.get('control_message_id'))
        return
    from aiogram.exceptions import TelegramAPIError
    try:
        person = await bot.get_chat(int(value))
        account = await backend.resolve(int(value), username=person.username,
            first_name=person.first_name, last_name=person.last_name)
        if account['status'] != 'approved':
            current = await backend.request('GET', f'/api/v1/accounts/{account["id"]}',
                                            telegram_user_id=message.from_user.id)
            await backend.request('PATCH', f'/api/v1/accounts/{account["id"]}',
                telegram_user_id=message.from_user.id, command=True,
                revision=current['revision'], body={'status': 'approved'})
        profiles = await backend.profiles(int(value))
        await _clear_flow(state)
        await show_admin_profile(message.chat.id, message.from_user.id, data.get('control_message_id'),
            profiles['items'][0]['id'], bot, backend, state)
    except (BackendError, TelegramAPIError):
        await render(bot, message.chat.id, Screen(tr(locale, 'profile.create.title'),
            (tr(locale, 'profile.create.telegram_failed'),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=AdminProfilesCallback().pack())]],
            state, data.get('control_message_id'))


@router.callback_query(AccountsCallback.filter())
async def accounts_cb(query: CallbackQuery, bot: Bot, backend: BackendClient,
                      state: FSMContext) -> None:
    await query.answer()
    await _clear_flow(state)
    locale = await _locale(state)
    page = await backend.accounts(query.from_user.id)
    rows = [[InlineKeyboardButton(
        text=f"{account.get('telegram_user_id') or account['id'][:8]} · "
             + tr(locale, 'status.' + account['status']),
        callback_data=AccountCallback(account_id=account['id']).pack())]
        for account in page['items']]
    rows.append([InlineKeyboardButton(text=tr(locale, 'back'), callback_data='admin_menu')])
    await render(bot, query.message.chat.id, Screen(tr(locale, 'accounts.title'),
        (tr(locale, 'accounts.choose'),) if page['items'] else (tr(locale, 'accounts.empty'),), embedded_buttons=True, navigation=True),
        rows, state, query.message.message_id)


@router.callback_query(AccountCallback.filter())
async def account_cb(query: CallbackQuery, callback_data: AccountCallback,
                     bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    await _clear_flow(state)
    account = await backend.request('GET', f'/api/v1/accounts/{callback_data.account_id}',
                                    telegram_user_id=query.from_user.id)
    locale = await _locale(state)
    rows = [[InlineKeyboardButton(text=tr(locale, 'accounts.new_profile'),
        callback_data=NewProfileCallback(account_id=account['id']).pack())],
        [InlineKeyboardButton(text=tr(locale, 'back'), callback_data=AccountsCallback().pack())]]
    await render(bot, query.message.chat.id,
        Screen(tr(locale, 'accounts.card', name=account.get('telegram_user_id') or account['id'][:8]),
               (tr(locale, 'account.status', status=tr(locale, f"status.{account['status']}")),
                tr(locale, 'accounts.role', role=tr(locale, f"role.{account['role']}"))), embedded_buttons=True, navigation=True),
        rows, state, query.message.message_id)


@router.callback_query(NewProfileCallback.filter())
async def new_profile_cb(query: CallbackQuery, callback_data: NewProfileCallback,
                         bot: Bot, state: FSMContext) -> None:
    await query.answer()
    await _clear_flow(state)
    await state.set_state(ProfileDraftState.waiting_for_name)
    await state.update_data(profile_account_id=callback_data.account_id,
                            profile_command_key=str(uuid4()))
    locale = await _locale(state)
    await render(bot, query.message.chat.id,
        Screen(tr(locale, 'profile.create.title'), (tr(locale, 'profile.create.name_prompt'),), embedded_buttons=True, navigation=True),
        [[InlineKeyboardButton(text=tr(locale, 'back'),
            callback_data=AccountCallback(account_id=callback_data.account_id).pack())]],
        state, query.message.message_id)


@router.message(ProfileDraftState.waiting_for_name, F.text)
async def process_profile_name(message: Message, bot: Bot, backend: BackendClient,
                               state: FSMContext) -> None:
    if message.from_user is None or message.chat.type != 'private':
        return
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    account_id = data.get('profile_account_id')
    message_id = data.get('control_message_id')
    name = message.text.strip()
    locale = await _locale(state)
    rows = [[InlineKeyboardButton(text=tr(locale, 'back'),
        callback_data=AccountCallback(account_id=account_id).pack())]] if account_id else []
    if not account_id or not name or len(name) > 128:
        await render(bot, message.chat.id, Screen(tr(locale, 'profile.create.title'),
            (tr(locale, 'profile.create.invalid_name'),), embedded_buttons=True, navigation=True), rows, state, message_id)
        return
    await state.set_state(None)
    await state.update_data(draft_profile_name=name,
                            draft_grants=data.get('draft_grants', []))
    await show_create_nodes(message.chat.id, message.from_user.id, message_id,
                            bot, backend, state)


async def show_create_nodes(chat_id: int, user_id: int, message_id: int,
                            bot: Bot, backend: BackendClient, state: FSMContext,
                            note: str | None = None) -> None:
    locale = await _locale(state)
    data = await state.get_data()
    nodes = await _all_nodes(backend, user_id)
    await state.update_data(draft_nodes=nodes)
    grants = {(item['node_key'], item['protocol']) for item in data.get('draft_grants', [])}
    from .user import server_page
    visible, page, pages = server_page(nodes, data.get('draft_nodes_page', 0))
    await state.update_data(draft_nodes_page=page)
    indices = {node['key']: index for index, node in enumerate(nodes)}
    def node_section(node):
        return Section(server_label(node),
        rows=((InlineKeyboardButton(text=tr(locale, 'profile.rich.select_protocols'),
            callback_data=f"profile_draft_node:{indices[node['key']]}",
            style='primary' if any(key == node['key'] for key, _ in grants) else None),),),
        divider_after=node['key'] != visible[-1]['key'], heading_size=4)
    nonce = data.get('draft_bulk_nonce') or secrets.token_urlsafe(6)
    scopes, groups = {}, {}
    for node in visible:
        groups.setdefault(node.get('region') or '', []).append(node)
    sections = [Section(tr(locale, 'profile.rich.bulk'),
        (tr(locale, 'profile.rich.bulk_note'),),
        heading_rows=(_bulk_buttons(nonce, 'all', locale, prefix='draft_bulk'),))]
    for region, items in groups.items():
        token = secrets.token_urlsafe(6)
        scopes[token] = region
        sections.append(Section(region or tr(locale, 'nodes.region_unknown'),
            sections=tuple(node_section(node) for node in items),
            heading_rows=(_bulk_buttons(nonce, token, locale, region=True, prefix='draft_bulk'),)))
    await state.update_data(draft_bulk_nonce=nonce, draft_bulk_regions=scopes)
    rows, arrows = [], []
    if page:
        arrows.append(InlineKeyboardButton(text='←', callback_data=f'profile_draft_page:{page - 1}'))
    if page + 1 < pages:
        arrows.append(InlineKeyboardButton(text='→', callback_data=f'profile_draft_page:{page + 1}'))
    if arrows:
        rows.append(arrows)
    rows.append([InlineKeyboardButton(text=tr(locale, 'back'),
        callback_data='profile_draft_name'),
        InlineKeyboardButton(text=tr(locale, 'profile.create.review'),
        callback_data='profile_draft_review')])
    lines = [tr(locale, 'profile.create.selected_name', name=data['draft_profile_name']),
             tr(locale, 'profile.create.choose_nodes')]
    if note:
        lines.insert(0, note)
    if pages > 1:
        lines.append(tr(locale, 'pagination.page', page=page + 1, pages=pages))
    await render(bot, chat_id, Screen(tr(locale, 'profile.create.title'), tuple(lines),
        sections=tuple(sections), embedded_buttons=True, navigation=True),
                 rows, state, message_id)


@router.callback_query(F.data.startswith('profile_draft_page:'))
async def draft_page_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    data = await state.get_data()
    if not data.get('profile_account_id') or not data.get('draft_profile_name'):
        return
    await state.update_data(draft_nodes_page=int(query.data.split(':', 1)[1]))
    await show_create_nodes(query.message.chat.id, query.from_user.id,
        query.message.message_id, bot, backend, state)


@router.callback_query(F.data.startswith('draft_bulk:'))
async def draft_bulk_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    _, nonce, action, scope = query.data.split(':', 3)
    data = await state.get_data()
    if (action not in {'add', 'del'} or data.get('draft_bulk_nonce') != nonce or
            not data.get('profile_account_id') or not data.get('draft_profile_name')):
        return
    if scope != 'all' and scope not in data.get('draft_bulk_regions', {}):
        await show_create_nodes(query.message.chat.id, query.from_user.id,
            query.message.message_id, bot, backend, state)
        return
    nodes = await _all_nodes(backend, query.from_user.id)
    region = None if scope == 'all' else data['draft_bulk_regions'][scope]
    await state.update_data(draft_grants=_bulk_grants(nodes, data.get('draft_grants', []), region, action == 'add'))
    await show_create_nodes(query.message.chat.id, query.from_user.id,
        query.message.message_id, bot, backend, state)


async def show_create_protocols(chat_id: int, user_id: int, message_id: int,
                                bot: Bot, state: FSMContext) -> None:
    locale = await _locale(state)
    data = await state.get_data()
    node = next((item for item in data.get('draft_nodes', [])
                 if item['key'] == data.get('draft_node_key')), None)
    if node is None:
        return
    enabled = {item['protocol'] for item in data.get('draft_grants', [])
               if item['node_key'] == node['key']}
    rows = [[InlineKeyboardButton(text=tr(locale, f'protocol.{protocol}'),
        callback_data=f'profile_draft_toggle:{protocol}',
        style='primary' if protocol in enabled else None) for protocol in node['protocols']]]
    rows.append([InlineKeyboardButton(text=tr(locale, 'back'),
        callback_data='profile_draft_nodes'),
        InlineKeyboardButton(text=tr(locale, 'profile.create.next'),
        callback_data='profile_draft_review')])
    await render(bot, chat_id, Screen(server_label(node),
        (tr(locale, 'profile.create.choose_protocols'),), embedded_buttons=True, navigation=True), rows, state, message_id)


@router.callback_query(F.data.startswith('profile_draft_node:'))
async def draft_node_cb(query: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    await query.answer()
    data = await state.get_data()
    try:
        index = int(query.data.split(':', 1)[1])
        node = data['draft_nodes'][index]
        if index < 0 or not data.get('draft_profile_name'):
            raise ValueError()
    except (KeyError, IndexError, ValueError):
        return
    await state.update_data(draft_node_key=node['key'])
    await show_create_protocols(query.message.chat.id, query.from_user.id,
                                query.message.message_id, bot, state)


@router.callback_query(F.data.startswith('profile_draft_toggle:'))
async def draft_toggle_cb(query: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    await query.answer()
    data = await state.get_data()
    node = next((item for item in data.get('draft_nodes', [])
                 if item['key'] == data.get('draft_node_key')), None)
    protocol = query.data.split(':', 1)[1]
    if node is None or protocol not in node['protocols']:
        return
    grant = {'node_key': node['key'], 'protocol': protocol}
    grants = [item for item in data.get('draft_grants', []) if item != grant]
    if grant not in data.get('draft_grants', []):
        grants.append(grant)
    await state.update_data(draft_grants=grants)
    await show_create_protocols(query.message.chat.id, query.from_user.id,
                                query.message.message_id, bot, state)


@router.callback_query(F.data == 'profile_draft_nodes')
async def draft_nodes_cb(query: CallbackQuery, bot: Bot, backend: BackendClient,
                         state: FSMContext) -> None:
    await query.answer()
    if not (await state.get_data()).get('draft_profile_name'):
        return
    await state.update_data(draft_node_key=None)
    await show_create_nodes(query.message.chat.id, query.from_user.id,
                            query.message.message_id, bot, backend, state)


async def show_create_review(chat_id: int, message_id: int, bot: Bot,
                             state: FSMContext, note: str | None = None) -> None:
    locale = await _locale(state)
    data = await state.get_data()
    nodes = {node['key']: server_label(node) for node in data.get('draft_nodes', [])}
    grouped = {}
    for grant in data.get('draft_grants', []):
        grouped.setdefault(grant['node_key'], []).append(tr(locale,
            f"protocol.{grant['protocol']}"))
    lines = [tr(locale, 'profile.create.selected_name',
                name=data.get('draft_profile_name') or '—')]
    sections = ()
    if grouped:
        sections = (Section(tr(locale, 'profile.rich.access'),
            (tr(locale, 'profile.rich.access_count', count=len(grouped)),),
            collapsed=True, tables=(Table((tr(locale, 'admin.nodes'), tr(locale, 'profile.rich.protocols')),
                tuple((nodes.get(key, key), ', '.join(protocols)) for key, protocols in grouped.items())),)),)
    if note:
        lines.insert(0, note)
    if not grouped:
        lines.append(tr(locale, 'profile.create.choose_one'))
    rows = [[InlineKeyboardButton(text=tr(locale, 'back'),
        callback_data='profile_draft_nodes')]]
    if grouped:
        rows[0].append(InlineKeyboardButton(text=tr(locale, 'profile.create.save'),
            callback_data='profile_draft_save', style='primary'))
    await render(bot, chat_id, Screen(tr(locale, 'profile.create.review_title'),
        tuple(lines), sections=sections, embedded_buttons=True, navigation=True), rows, state, message_id)


@router.callback_query(F.data == 'profile_draft_review')
async def draft_review_cb(query: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    await query.answer()
    data = await state.get_data()
    if not data.get('draft_profile_name') or not data.get('profile_account_id'):
        return
    await show_create_review(query.message.chat.id,
                             query.message.message_id, bot, state)


@router.callback_query(F.data == 'profile_draft_name')
async def draft_name_cb(query: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    await query.answer()
    data = await state.get_data()
    if not data.get('profile_account_id'):
        return
    await state.set_state(ProfileDraftState.waiting_for_name)
    locale = await _locale(state)
    await render(bot, query.message.chat.id, Screen(tr(locale, 'profile.create.title'),
        (tr(locale, 'profile.create.current_name', name=data.get('draft_profile_name') or '—'),
         tr(locale, 'profile.create.name_prompt')), embedded_buttons=True, navigation=True),
        [[InlineKeyboardButton(text=tr(locale, 'back'),
            callback_data=AccountCallback(account_id=data['profile_account_id']).pack())]],
        state, query.message.message_id)


@router.callback_query(F.data == 'profile_draft_save')
async def draft_save_cb(query: CallbackQuery, bot: Bot, backend: BackendClient,
                        state: FSMContext) -> None:
    await query.answer()
    data = await state.get_data()
    if not data.get('draft_profile_name') or not data.get('profile_account_id'):
        return
    locale = await _locale(state)
    grants = data.get('draft_grants', [])
    if not grants:
        await show_create_nodes(query.message.chat.id, query.from_user.id,
            query.message.message_id, bot, backend, state,
            note=tr(locale, 'profile.create.choose_one'))
        return
    try:
        result = await backend.create_profile(query.from_user.id,
            data['profile_account_id'], data['draft_profile_name'],
            data['profile_command_key'], grants)
    except BackendError as exc:
        await show_create_review(query.message.chat.id,
            query.message.message_id, bot, state, note=_profile_error(locale, exc))
        return
    await _clear_flow(state)
    await show_admin_profile(query.message.chat.id, query.from_user.id,
        query.message.message_id, result['profile']['id'], bot, backend, state)


@router.callback_query(AdminProfilesCallback.filter())
async def admin_profiles_cb(query: CallbackQuery, bot: Bot,
                            backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    await state.set_state(None)
    data = await state.get_data()
    await show_admin_profiles(query.message.chat.id, query.from_user.id,
        query.message.message_id, bot, backend, state,
        page_index=data.get('admin_profile_page', 0))


async def show_admin_profiles(chat_id: int, user_id: int, message_id: int,
                              bot: Bot, backend: BackendClient, state: FSMContext,
                              page_index: int = 0) -> None:
    locale = await _locale(state)
    data = await state.get_data()
    cursors = list(data.get('admin_profile_cursors') or [None])
    if page_index < 0 or page_index >= len(cursors):
        page_index = 0
    search = data.get('admin_profile_search')
    page = await backend.admin_profiles(user_id, cursor=cursors[page_index],
                                        search=search)
    if page.get('next_cursor'):
        if len(cursors) == page_index + 1:
            cursors.append(page['next_cursor'])
        else:
            cursors[page_index + 1] = page['next_cursor']
    else:
        cursors = cursors[:page_index + 1]
    await state.update_data(admin_profile_cursors=cursors,
                            admin_profile_page=page_index)
    controls = [InlineKeyboardButton(text=tr(locale, 'profiles.admin.new'),
                callback_data='profile_add_user'),
                InlineKeyboardButton(text=tr(locale, 'profiles.admin.search'),
                callback_data='search_profile')]
    if search:
        controls.append(InlineKeyboardButton(text=tr(locale, 'profiles.admin.show_all'),
            callback_data='admin_profiles_all'))
    sections = [Section(tr(locale, 'admin.rich.management'),
        (tr(locale, 'requests.search_active', query=search),) if search else (),
        (tuple(controls[:2]), *(((controls[2],),) if search else ())))]
    for index, item in enumerate(page['items']):
        sections.append(Section(item['display_name'],
            (tr(locale, 'profile.admin.status', status=_status(item, locale)),),
            ((InlineKeyboardButton(text=tr(locale, 'profile.rich.open'),
                callback_data=AdminProfileCallback(profile_id=item['id']).pack()),),),
            divider_after=index < len(page['items']) - 1))
    rows = []
    arrows = []
    if page_index > 0:
        arrows.append(InlineKeyboardButton(text='←',
            callback_data=f'admin_profiles_page:{page_index - 1}'))
    if page.get('next_cursor'):
        arrows.append(InlineKeyboardButton(text='→',
            callback_data=f'admin_profiles_page:{page_index + 1}'))
    if arrows:
        rows.append(arrows)
    rows.append([InlineKeyboardButton(text=tr(locale, 'back'), callback_data='admin_menu')])
    lines = [tr(locale, 'profiles.admin.description') if page['items'] else
             tr(locale, 'profiles.search.empty' if search else 'profiles.admin.empty')]
    if page_index or page.get('next_cursor'):
        lines.append(tr(locale, 'profiles.admin.page', page=page_index + 1))
    await render(bot, chat_id, Screen(tr(locale, 'profiles.admin.title'), tuple(lines),
        sections=tuple(sections), embedded_buttons=True, navigation=True),
                 rows, state, message_id)


@router.callback_query(F.data.startswith('admin_profiles_page:'))
async def admin_profiles_page_cb(query: CallbackQuery, bot: Bot,
                                 backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    try:
        page_index = int(query.data.split(':', 1)[1])
    except ValueError:
        return
    await show_admin_profiles(query.message.chat.id, query.from_user.id,
        query.message.message_id, bot, backend, state, page_index=page_index)


@router.callback_query(F.data == 'admin_profiles_all')
async def admin_profiles_all_cb(query: CallbackQuery, bot: Bot,
                                backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    await state.update_data(admin_profile_search=None, admin_profile_cursors=[None],
                            admin_profile_page=0)
    await show_admin_profiles(query.message.chat.id, query.from_user.id,
        query.message.message_id, bot, backend, state)


@router.callback_query(F.data == 'search_profile')
async def search_profile_cb(query: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    await query.answer()
    await state.set_state(ProfileSearchState.waiting_for_query)
    locale = await _locale(state)
    await render(bot, query.message.chat.id, Screen(tr(locale, 'profiles.search.title'),
        (tr(locale, 'profiles.search.prompt'),), embedded_buttons=True, navigation=True),
        [[InlineKeyboardButton(text=tr(locale, 'back'),
            callback_data=AdminProfilesCallback().pack())]],
        state, query.message.message_id)


@router.message(ProfileSearchState.waiting_for_query, F.text)
async def process_profile_search(message: Message, bot: Bot,
                                 backend: BackendClient, state: FSMContext) -> None:
    if message.from_user is None or message.chat.type != 'private':
        return
    try:
        await message.delete()
    except Exception:
        pass
    text = message.text.strip()
    data = await state.get_data()
    message_id = data.get('control_message_id')
    locale = await _locale(state)
    if not 1 <= len(text) <= 128:
        await render(bot, message.chat.id, Screen(tr(locale, 'profiles.search.title'),
            (tr(locale, 'profiles.search.invalid'),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'),
                callback_data=AdminProfilesCallback().pack())]], state, message_id)
        return
    await state.set_state(None)
    await state.update_data(admin_profile_search=text, admin_profile_cursors=[None],
                            admin_profile_page=0)
    await show_admin_profiles(message.chat.id, message.from_user.id, message_id,
                              bot, backend, state)


@router.callback_query(AdminProfileCallback.filter())
async def admin_profile_cb(query: CallbackQuery, callback_data: AdminProfileCallback,
                           bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    await show_admin_profile(query.message.chat.id, query.from_user.id,
                             query.message.message_id, callback_data.profile_id,
                             bot, backend, state)


async def show_admin_profile(chat_id: int, user_id: int, message_id: int,
                             profile_id: str, bot: Bot, backend: BackendClient,
                             state: FSMContext, *, access_page: int = 0, access_open: bool = False) -> None:
    profile, grant_page, nodes, operation = await asyncio.gather(
        backend.request('GET', f'/api/v1/profiles/{profile_id}', telegram_user_id=user_id),
        backend.profile_grants(user_id, profile_id), _all_nodes(backend, user_id),
        backend.profile_operation(user_id, profile_id))
    grants = grant_page['items']
    node_names = {node['key']: node for node in nodes}
    locale = await _locale(state)
    await state.set_state(None)
    await state.update_data(edit_profile_id=None, draft_grants=None,
                            original_grants=None, edit_profile_revision=None, grant_nodes_page=0)
    grouped = {}
    for grant in grants:
        grouped.setdefault(grant['node_key'], []).append(tr(locale, f"protocol.{grant['protocol']}"))
    tasks = operation['tasks'] if operation else []
    operation_status = operation['status'] if operation else 'no_targets'
    lines = [tr(locale, 'profile.admin.status', status=_status(profile, locale)),
             tr(locale, 'profile.admin.cleanup' if profile.get('deleting') else
                'profile.admin.provision',
                status=tr(locale, f'operation.{operation_status}'),
                ready=sum(task['status'] == 'succeeded' for task in tasks), total=len(tasks))]
    if profile.get('deleting') and operation_status == 'blocked':
        lines.append(tr(locale, 'profile.admin.cleanup_blocked'))
    from .user import server_page
    access_nodes = [node_names.get(key, {'key': key, 'title': key, 'region': '', 'flag': ''})
                    for key in grouped]
    visible, access_page, pages = server_page(access_nodes, access_page)
    region_groups = {}
    for node in visible:
        region_groups.setdefault(node.get('region') or tr(locale, 'nodes.region_unknown'), []).append(node)
    tables = tuple(Section(region, tables=(Table(
        (tr(locale, 'admin.nodes'), tr(locale, 'profile.rich.protocols')),
        tuple((server_label(node), ', '.join(grouped[node['key']])) for node in items)),))
        for region, items in region_groups.items())
    pagination = []
    if access_page:
        pagination.append(InlineKeyboardButton(text='←', callback_data=f'prof_access_page:{profile_id}:{access_page - 1}'))
    if access_page + 1 < pages:
        pagination.append(InlineKeyboardButton(text='→', callback_data=f'prof_access_page:{profile_id}:{access_page + 1}'))
    access_lines = (tr(locale, 'profile.rich.access_count', count=len(access_nodes)),)
    if pages > 1:
        access_lines += (tr(locale, 'pagination.page', page=access_page + 1, pages=pages),)
    if not access_nodes:
        access_lines = (tr(locale, 'profile.admin.no_grants'),)
    sections = [Section(tr(locale, 'profile.rich.access'),
        rows=((InlineKeyboardButton(text=tr(locale, 'profile.rich.manage_access'),
            callback_data=GrantNodesCallback(profile_id=profile_id).pack()),),) if not profile.get('deleting') else (),
        sections=(Section(tr(locale, 'profile.rich.servers'), access_lines,
            (tuple(pagination),) if pagination else (), collapsed=True, is_open=access_open,
            sections=tables),))]
    if not profile.get('deleting'):
        sections.extend((Section(tr(locale, 'profile.admin.status_title'),
            (tr(locale, 'profile.rich.sync_note'),), (_status_buttons(profile, locale),)),
            Section(tr(locale, 'profile.rich.identity'), rows=((InlineKeyboardButton(
                text=tr(locale, 'profile.admin.rename'), callback_data=f'admin_profile_rename:{profile_id}'),),)),
            Section(tr(locale, 'profile.rich.management'), rows=((InlineKeyboardButton(
                text=tr(locale, 'profile.admin.delete'), callback_data=f'prof_del:{profile_id}', style='danger'),),), collapsed=True)))
    details = (tr(locale, 'profile.admin.id', id=profile['id']),
        tr(locale, 'profile.rich.owner', value=profile.get('owner_account_id') or '—'),
        tr(locale, 'profile.rich.revision', value=profile['desired_revision']),
        tr(locale, 'profile.rich.expires', value=profile.get('expires_at') or '—'))
    sections.append(Section(tr(locale, 'requests.details'), details, collapsed=True))
    if operation:
        sections.insert(0, Section(tr(locale, 'profile.rich.synchronization'), rows=((
            InlineKeyboardButton(text=tr(locale, 'profile.rich.operation'),
                callback_data=f'prof_op:{profile_id}'),),)))
    rows = [[InlineKeyboardButton(text=tr(locale, 'profile.admin.refresh'),
        callback_data=AdminProfileCallback(profile_id=profile_id).pack())],
        [InlineKeyboardButton(text=tr(locale, 'back'),
            callback_data=AdminProfilesCallback().pack())]]
    await render(bot, chat_id, Screen(profile['display_name'], tuple(lines),
        sections=tuple(sections), embedded_buttons=True, navigation=True),
        rows, state, message_id)


@router.callback_query(F.data.startswith('prof_access_page:'))
async def profile_access_page_cb(query: CallbackQuery, bot: Bot, backend: BackendClient,
                                  state: FSMContext):
    await query.answer()
    _, profile_id, page = query.data.split(':', 2)
    await show_admin_profile(query.message.chat.id, query.from_user.id,
        query.message.message_id, profile_id, bot, backend, state, access_page=int(page), access_open=True)


@router.callback_query(F.data.startswith('prof_state:'))
async def set_profile_state_cb(query: CallbackQuery, bot: Bot, backend: BackendClient,
                                state: FSMContext):
    _, profile_id, selected = query.data.split(':', 2)
    if selected not in {'active', 'frozen'}:
        await query.answer()
        return
    profile = await backend.request('GET', f'/api/v1/profiles/{profile_id}',
                                    telegram_user_id=query.from_user.id)
    if not profile.get('deleting') and profile['frozen'] != (selected == 'frozen'):
        try:
            await backend.edit_profile(query.from_user.id, profile_id, profile['desired_revision'],
                                       {'frozen': selected == 'frozen'})
        except BackendError as exc:
            await query.answer(_profile_error(await _locale(state), exc), show_alert=True)
            return
    await query.answer()
    await show_admin_profile(query.message.chat.id, query.from_user.id,
        query.message.message_id, profile_id, bot, backend, state)


@router.callback_query(F.data.startswith('prof_op:'))
async def profile_operation_cb(query: CallbackQuery, bot: Bot, backend: BackendClient,
                                state: FSMContext):
    await query.answer()
    profile_id = query.data.split(':', 1)[1]
    operation = await backend.profile_operation(query.from_user.id, profile_id)
    locale = await _locale(state)
    status = operation['status'] if operation else 'no_targets'
    sections = []
    if operation:
        nodes = {node['key']: server_label(node) for node in await _all_nodes(backend, query.from_user.id)}
        sections.append(Section(tr(locale, 'profile.rich.targets'), tables=(Table(
            (tr(locale, 'admin.nodes'), tr(locale, 'profile.rich.protocols'), tr(locale, 'admin.rich.value')),
            tuple((nodes.get(task['node_key'], task['node_key']), tr(locale, 'protocol.' + task['protocol']),
                _task_status(task['status'], locale))
                for task in operation['tasks'])),)))
    await render(bot, query.message.chat.id, Screen(tr(locale, 'profile.rich.synchronization'),
        (tr(locale, 'profile.rich.operation_state', value=tr(locale, 'operation.' + status)),
         tr(locale, 'profile.rich.sync_note')), sections=tuple(sections), embedded_buttons=True, navigation=True),
        [[InlineKeyboardButton(text=tr(locale, 'profile.admin.refresh'), callback_data=f'prof_op:{profile_id}')],
         [InlineKeyboardButton(text=tr(locale, 'back'), callback_data=AdminProfileCallback(profile_id=profile_id).pack())]],
        state, query.message.message_id)


@router.callback_query(F.data.startswith('admin_profile_edit:'))
async def admin_profile_edit_cb(query: CallbackQuery, bot: Bot,
                                backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    await state.set_state(None)
    await state.update_data(delete_profile_id=None,
                            delete_profile_revision=None,
                            delete_profile_key=None)
    await show_profile_edit_menu(query.message.chat.id, query.from_user.id,
        query.message.message_id, query.data.split(':', 1)[1], bot, backend, state)


async def show_profile_edit_menu(chat_id: int, user_id: int, message_id: int,
                                 profile_id: str, bot: Bot, backend: BackendClient,
                                 state: FSMContext) -> None:
    # Old callbacks remain valid; editing now lives directly on the profile card.
    await show_admin_profile(chat_id, user_id, message_id, profile_id, bot, backend, state)


@router.callback_query(F.data.startswith('prof_del:'))
async def delete_profile_confirm_cb(query: CallbackQuery, bot: Bot,
                                    backend: BackendClient,
                                    state: FSMContext) -> None:
    await query.answer()
    profile_id = query.data.split(':', 1)[1]
    profile = await backend.request('GET', f'/api/v1/profiles/{profile_id}',
                                    telegram_user_id=query.from_user.id)
    if profile.get('deleting'):
        await show_admin_profile(query.message.chat.id, query.from_user.id,
            query.message.message_id, profile_id, bot, backend, state)
        return
    await state.update_data(delete_profile_id=profile_id,
                            delete_profile_revision=profile['desired_revision'],
                            delete_profile_key=str(uuid4()))
    locale = await _locale(state)
    rows = [[InlineKeyboardButton(text=tr(locale, 'profile.admin.delete_confirm'),
        callback_data=f'prof_del_go:{profile_id}', style='danger')],
        [InlineKeyboardButton(text=tr(locale, 'back'),
        callback_data=f'admin_profile_edit:{profile_id}')]]
    await render(bot, query.message.chat.id,
        Screen(tr(locale, 'profile.admin.delete_title'),
            (tr(locale, 'profile.admin.name', name=profile['display_name']),
             tr(locale, 'profile.admin.delete_warning')), embedded_buttons=True, navigation=True),
        rows, state, query.message.message_id)


@router.callback_query(F.data.startswith('prof_del_go:'))
async def delete_profile_cb(query: CallbackQuery, bot: Bot,
                            backend: BackendClient, state: FSMContext) -> None:
    profile_id = query.data.split(':', 1)[1]
    data = await state.get_data()
    if data.get('delete_profile_id') != profile_id:
        await query.answer()
        return
    try:
        result = await backend.delete_profile(query.from_user.id, profile_id,
            data['delete_profile_revision'], data['delete_profile_key'])
    except BackendError as exc:
        await query.answer(_profile_error(await _locale(state), exc), show_alert=True)
        return
    await query.answer()
    await state.update_data(delete_profile_id=None,
                            delete_profile_revision=None,
                            delete_profile_key=None)
    await refresh_deleted_owner(result, query.from_user.id, bot, backend, state)
    locale = await _locale(state)
    status = result['runtime_status']
    lines = (tr(locale, 'profile.admin.delete_queued',
                status=tr(locale, f'operation.{status}')),
             tr(locale, 'profile.admin.delete_followup'))
    rows = [[InlineKeyboardButton(text=tr(locale, 'profile.admin.open_status'),
        callback_data=AdminProfileCallback(profile_id=profile_id).pack())],
        [InlineKeyboardButton(text=tr(locale, 'back'),
        callback_data=AdminProfilesCallback().pack())]]
    await render(bot, query.message.chat.id,
        Screen(tr(locale, 'profile.admin.delete_title'), lines, embedded_buttons=True, navigation=True),
        rows, state, query.message.message_id)


async def refresh_deleted_owner(result, admin_id, bot, backend, state):
    """Drop a revoked member's cached menu and replace their existing screen."""
    owner = result['profile'].get('owner_account_id')
    if not owner:
        return
    try:
        account = await backend.request('GET', f'/api/v1/accounts/{owner}',
                                        telegram_user_id=admin_id)
        recipient = account.get('telegram_user_id')
        if account.get('status') != 'pending' or not recipient:
            return
        member_state = FSMContext(storage=state.storage,
            key=replace(state.key, chat_id=recipient, user_id=recipient,
                        thread_id=None, business_connection_id=None))
        await member_state.set_state(None)
        await member_state.update_data(home_presentation=None, issuance_poll_token=None)
        from .user import show_home
        await show_home(recipient, recipient, bot, backend, member_state, account=account)
    except (BackendError, TelegramAPIError):
        logging.getLogger(__name__).warning('Could not refresh revoked member screen', exc_info=True)


@router.callback_query(F.data.startswith('admin_profile_rename:'))
async def admin_profile_rename_cb(query: CallbackQuery, bot: Bot,
                                  state: FSMContext) -> None:
    await query.answer()
    profile_id = query.data.split(':', 1)[1]
    await state.set_state(ProfileRenameState.waiting_for_name)
    await state.update_data(rename_profile_id=profile_id)
    locale = await _locale(state)
    await render(bot, query.message.chat.id, Screen(
        tr(locale, 'profile.admin.rename_title'),
        (tr(locale, 'profile.admin.rename_prompt'),), embedded_buttons=True, navigation=True),
        [[InlineKeyboardButton(text=tr(locale, 'back'),
            callback_data=f'admin_profile_edit:{profile_id}')]],
        state, query.message.message_id)


@router.message(ProfileRenameState.waiting_for_name, F.text)
async def admin_profile_rename_message(message: Message, bot: Bot,
                                       backend: BackendClient,
                                       state: FSMContext) -> None:
    if message.from_user is None or message.chat.type != 'private':
        return
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    profile_id = data.get('rename_profile_id')
    if not profile_id:
        return
    name = message.text.strip()
    locale = await _locale(state)
    if not name or len(name) > 128:
        await render(bot, message.chat.id, Screen(
            tr(locale, 'profile.admin.rename_title'),
            (tr(locale, 'profile.create.invalid_name'),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'),
                callback_data=f'admin_profile_edit:{profile_id}')]],
            state, data.get('control_message_id'))
        return
    profile = await backend.request('GET', f'/api/v1/profiles/{profile_id}',
                                    telegram_user_id=message.from_user.id)
    try:
        await backend.edit_profile(message.from_user.id, profile_id,
                                   profile['desired_revision'], {'display_name': name})
    except BackendError as exc:
        await render(bot, message.chat.id, Screen(
            tr(locale, 'profile.admin.rename_title'),
            (_profile_error(locale, exc),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'),
                callback_data=f'admin_profile_edit:{profile_id}')]],
            state, data.get('control_message_id'))
        return
    await state.set_state(None)
    await show_profile_edit_menu(message.chat.id, message.from_user.id,
        data.get('control_message_id'), profile_id, bot, backend, state)


@router.callback_query(F.data.startswith('admin_profile_status:'))
async def admin_profile_status_cb(query: CallbackQuery, bot: Bot,
                                  backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    await show_profile_status(query.message.chat.id, query.from_user.id,
        query.message.message_id, query.data.split(':', 1)[1], bot, backend, state)


async def show_profile_status(chat_id: int, user_id: int, message_id: int,
                              profile_id: str, bot: Bot, backend: BackendClient,
                              state: FSMContext) -> None:
    profile = await backend.request('GET', f'/api/v1/profiles/{profile_id}',
                                    telegram_user_id=user_id)
    locale = await _locale(state)
    rows = [list(_status_buttons(profile, locale)),
        [InlineKeyboardButton(text=tr(locale, 'back'),
        callback_data=f'admin_profile_edit:{profile_id}')]]
    await render(bot, chat_id, Screen(tr(locale, 'profile.admin.status_title'),
        (tr(locale, 'profile.admin.status', status=_status(profile, locale)),
         tr(locale, 'profile.rich.sync_note')), embedded_buttons=True, navigation=True),
        rows, state, message_id)


@router.callback_query(GrantNodesCallback.filter())
async def grant_nodes_cb(query: CallbackQuery, callback_data: GrantNodesCallback,
                         bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    await show_grant_nodes(query.message.chat.id, query.from_user.id,
                           query.message.message_id, callback_data.profile_id,
                           bot, backend, state)


async def show_grant_nodes(chat_id: int, user_id: int, message_id: int,
                           profile_id: str, bot: Bot, backend: BackendClient,
                           state: FSMContext) -> None:
    data = await _ensure_grant_draft(backend, user_id, profile_id, state)
    nodes = await _all_nodes(backend, user_id)
    locale = await _locale(state)
    from .user import server_page
    visible, page, pages = server_page(nodes, data.get('grant_nodes_page', 0))
    await state.update_data(grant_nodes_page=page, grant_inline=True)
    granted = {(item['node_key'], item['protocol']) for item in data['draft_grants']}
    def node_section(node):
        buttons = []
        for protocol in node['protocols']:
            selected = (node['key'], protocol) in granted
            if not selected and not node.get('enabled', True):
                continue
            callback = (RemoveGrantCallback if selected else AddGrantCallback)(
                profile_id=profile_id, node_key=node['key'], protocol=protocol)
            buttons.append(InlineKeyboardButton(text=tr(locale, 'protocol.' + protocol),
                callback_data=callback.pack(), style='primary' if selected else None))
        return Section(server_label(node), rows=(tuple(buttons),) if buttons else (),
            lines=() if buttons else (tr(locale, 'profile.rich.no_protocols'),),
            divider_after=node['key'] != visible[-1]['key'], heading_size=4)
    scopes, groups, with_controls = {}, {}, []
    for node in visible:
        groups.setdefault(node.get('region') or '', []).append(node)
    for raw_region, items in groups.items():
        # Bind raw region names to short opaque tokens; names may be Unicode
        # and must not exceed Telegram's callback length or shift by list index.
        token = secrets.token_urlsafe(6)
        scopes[token] = raw_region
        with_controls.append(Section(raw_region or tr(locale, 'nodes.region_unknown'),
            sections=tuple(node_section(node) for node in items),
            heading_rows=(_bulk_buttons(profile_id, token, locale, region=True),)))
    await state.update_data(grant_bulk_regions=scopes)
    sections = (Section(tr(locale, 'profile.rich.bulk'),
        (tr(locale, 'profile.rich.bulk_note'),),
        heading_rows=(_bulk_buttons(profile_id, 'all', locale),)), *with_controls)
    changed = set(granted) != {(item['node_key'], item['protocol']) for item in data['original_grants']}
    lines = [tr(locale, 'profile.rich.draft_note'),
             tr(locale, 'profile.rich.unsaved' if changed else 'profile.rich.no_changes')]
    if pages > 1:
        lines.append(tr(locale, 'pagination.page', page=page + 1, pages=pages))
    if not nodes:
        lines.append(tr(locale, 'nodes.admin.empty'))
    rows, arrows = [], []
    if page:
        arrows.append(InlineKeyboardButton(text='←', callback_data=f'grant_page:{profile_id}:{page - 1}'))
    if page + 1 < pages:
        arrows.append(InlineKeyboardButton(text='→', callback_data=f'grant_page:{profile_id}:{page + 1}'))
    if arrows:
        rows.append(arrows)
    if changed:
        rows.append([InlineKeyboardButton(text=tr(locale, 'profile.admin.save'),
            callback_data=f'admin_profile_grants_save:{profile_id}', style='primary')])
    rows.append([InlineKeyboardButton(text=tr(locale, 'back'),
        callback_data=AdminProfileCallback(profile_id=profile_id).pack())])
    await render(bot, chat_id, Screen(tr(locale, 'profile.admin.nodes_title'),
        tuple(lines), sections=sections, embedded_buttons=True, navigation=True), rows, state, message_id)


@router.callback_query(F.data.startswith('grant_page:'))
async def grant_page_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    _, profile_id, page = query.data.split(':', 2)
    # Do not let a stale page from a different profile corrupt the current draft.
    await _ensure_grant_draft(backend, query.from_user.id, profile_id, state)
    await state.update_data(grant_nodes_page=int(page))
    await show_grant_nodes(query.message.chat.id, query.from_user.id,
        query.message.message_id, profile_id, bot, backend, state)


@router.callback_query(F.data.startswith('grant_bulk:'))
async def grant_bulk_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    _, profile_id, action, scope = query.data.split(':', 3)
    if action not in {'add', 'del'}:
        return
    data = await state.get_data()
    # Never apply a bulk command from an old screen to a different/new draft.
    if data.get('edit_profile_id') != profile_id:
        await show_grant_nodes(query.message.chat.id, query.from_user.id,
            query.message.message_id, profile_id, bot, backend, state)
        return
    if scope != 'all' and scope not in data.get('grant_bulk_regions', {}):
        await show_grant_nodes(query.message.chat.id, query.from_user.id,
            query.message.message_id, profile_id, bot, backend, state)
        return
    region = None if scope == 'all' else data['grant_bulk_regions'][scope]
    nodes = await _all_nodes(backend, query.from_user.id)
    await state.update_data(draft_grants=_bulk_grants(nodes, data['draft_grants'], region, action == 'add'))
    await show_grant_nodes(query.message.chat.id, query.from_user.id,
        query.message.message_id, profile_id, bot, backend, state)


@router.callback_query(GrantProtocolsCallback.filter())
async def grant_protocols_cb(query: CallbackQuery, callback_data: GrantProtocolsCallback,
                             bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    await show_grant_protocols(query.message.chat.id, query.from_user.id,
        query.message.message_id, callback_data.profile_id, callback_data.node_key,
        bot, backend, state)


async def show_grant_protocols(chat_id: int, user_id: int, message_id: int,
                               profile_id: str, node_key: str, bot: Bot,
                               backend: BackendClient, state: FSMContext) -> None:
    data = await _ensure_grant_draft(backend, user_id, profile_id, state)
    await state.update_data(grant_inline=False)
    enabled = {item['protocol'] for item in data['draft_grants'] if item['node_key'] == node_key}
    locale = await _locale(state)
    node = await backend.request('GET', f'/api/v1/nodes/{node_key}',
                                 telegram_user_id=user_id)
    buttons = []
    for protocol in node['protocols']:
        callback = (RemoveGrantCallback if protocol in enabled else AddGrantCallback)(
            profile_id=profile_id, node_key=node_key, protocol=protocol)
        buttons.append(InlineKeyboardButton(
            text=tr(locale, 'protocol.' + protocol),
            callback_data=callback.pack(), style='primary' if protocol in enabled else None))
    rows = [buttons]
    rows.append([InlineKeyboardButton(text=tr(locale, 'back'),
        callback_data=GrantNodesCallback(profile_id=profile_id).pack())])
    await render(bot, chat_id, Screen(tr(locale, 'profile.admin.protocols_title'),
        (tr(locale, 'profile.admin.server', name=server_label(node)),
         tr(locale, 'profile.admin.protocols_prompt')), embedded_buttons=True, navigation=True),
        rows, state, message_id)


async def change_grant(query: CallbackQuery, profile_id: str, node_key: str,
                       protocol: str, add: bool, bot: Bot,
                       backend: BackendClient, state: FSMContext) -> None:
    user_id = query.from_user.id
    data = await _ensure_grant_draft(backend, user_id, profile_id, state)
    grants = list(data['draft_grants'])
    target = {'node_key': node_key, 'protocol': protocol}
    if add and target not in grants:
        grants.append(target)
    elif not add:
        grants = [item for item in grants if item != target]
    await state.update_data(edit_profile_id=profile_id, draft_grants=grants)
    if data.get('grant_inline'):
        await show_grant_nodes(query.message.chat.id, user_id,
            query.message.message_id, profile_id, bot, backend, state)
    else:
        await show_grant_protocols(query.message.chat.id, user_id,
            query.message.message_id, profile_id, node_key, bot, backend, state)


@router.callback_query(F.data.startswith('admin_profile_grants_save:'))
async def save_grants_cb(query: CallbackQuery, bot: Bot,
                         backend: BackendClient, state: FSMContext) -> None:
    profile_id = query.data.split(':', 1)[1]
    data = await state.get_data()
    if data.get('edit_profile_id') != profile_id:
        await query.answer()
        await show_grant_nodes(query.message.chat.id, query.from_user.id,
                               query.message.message_id, profile_id, bot, backend, state)
        return
    try:
        await backend.replace_grants(query.from_user.id, profile_id,
                                     data['edit_profile_revision'], data['draft_grants'])
    except BackendError as exc:
        await query.answer(_profile_error(await _locale(state), exc), show_alert=True)
        return
    await query.answer()
    await state.update_data(edit_profile_id=None, draft_grants=None)
    await show_admin_profile(query.message.chat.id, query.from_user.id,
                             query.message.message_id, profile_id, bot, backend, state)


@router.callback_query(AddGrantCallback.filter())
async def add_grant_cb(query: CallbackQuery, callback_data: AddGrantCallback,
                       bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    await change_grant(query, callback_data.profile_id, callback_data.node_key,
        callback_data.protocol, True, bot, backend, state)


@router.callback_query(RemoveGrantCallback.filter())
async def remove_grant_cb(query: CallbackQuery, callback_data: RemoveGrantCallback,
                          bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    await change_grant(query, callback_data.profile_id, callback_data.node_key,
        callback_data.protocol, False, bot, backend, state)


@router.callback_query(ToggleFreezeCallback.filter())
async def toggle_freeze_cb(query: CallbackQuery, callback_data: ToggleFreezeCallback,
                           bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    profile = await backend.request('GET', f'/api/v1/profiles/{callback_data.profile_id}',
                                    telegram_user_id=query.from_user.id)
    await backend.edit_profile(query.from_user.id, callback_data.profile_id,
        profile['desired_revision'], {'frozen': not profile['frozen']})
    await show_profile_status(query.message.chat.id, query.from_user.id,
        query.message.message_id, callback_data.profile_id, bot, backend, state)
