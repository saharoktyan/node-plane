"""Profile administration backed by the standalone backend API."""
from __future__ import annotations

from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from ..backend import BackendClient, BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen
from .callbacks import (AccountsCallback, AccountCallback, NewProfileCallback,
    AdminProfilesCallback, AdminProfileCallback, GrantNodesCallback,
    GrantProtocolsCallback, AddGrantCallback, RemoveGrantCallback,
    ToggleFreezeCallback)
from .common import render
from .states import ProfileDraftState

router = Router()


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
        (tr(locale, 'accounts.choose'),) if page['items'] else (tr(locale, 'accounts.empty'),)),
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
                tr(locale, 'accounts.role', role=tr(locale, f"role.{account['role']}")))),
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
        Screen(tr(locale, 'profile.create.title'), (tr(locale, 'profile.create.name_prompt'),)),
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
            (tr(locale, 'profile.create.invalid_name'),)), rows, state, message_id)
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
    nodes = (await backend.admin_nodes(user_id))['items']
    await state.update_data(draft_nodes=nodes)
    grants = {(item['node_key'], item['protocol']) for item in data.get('draft_grants', [])}
    rows = [[InlineKeyboardButton(
        text=f"{'✅' if any(key == node['key'] for key, _ in grants) else '○'} {node['title']}",
        callback_data=f'profile_draft_node:{index}')]
        for index, node in enumerate(nodes)]
    rows.append([InlineKeyboardButton(text=tr(locale, 'back'),
        callback_data='profile_draft_name'),
        InlineKeyboardButton(text=tr(locale, 'profile.create.review'),
        callback_data='profile_draft_review')])
    lines = [tr(locale, 'profile.create.selected_name', name=data['draft_profile_name']),
             tr(locale, 'profile.create.choose_nodes')]
    if note:
        lines.insert(0, note)
    await render(bot, chat_id, Screen(tr(locale, 'profile.create.title'), tuple(lines)),
                 rows, state, message_id)


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
    rows = [[InlineKeyboardButton(
        text=f"{'✅' if protocol in enabled else '○'} {tr(locale, f'protocol.{protocol}')}",
        callback_data=f'profile_draft_toggle:{protocol}')]
        for protocol in node['protocols']]
    rows.append([InlineKeyboardButton(text=tr(locale, 'back'),
        callback_data='profile_draft_nodes'),
        InlineKeyboardButton(text=tr(locale, 'profile.create.next'),
        callback_data='profile_draft_review')])
    await render(bot, chat_id, Screen(node['title'],
        (tr(locale, 'profile.create.choose_protocols'),)), rows, state, message_id)


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
    nodes = {node['key']: node['title'] for node in data.get('draft_nodes', [])}
    grouped = {}
    for grant in data.get('draft_grants', []):
        grouped.setdefault(grant['node_key'], []).append(tr(locale,
            f"protocol.{grant['protocol']}"))
    lines = [tr(locale, 'profile.create.selected_name',
                name=data.get('draft_profile_name') or '—')]
    lines.extend(tr(locale, 'profile.create.review_grant',
        node=nodes.get(node_key, node_key), protocols=', '.join(protocols))
        for node_key, protocols in grouped.items())
    if note:
        lines.insert(0, note)
    if not grouped:
        lines.append(tr(locale, 'profile.create.choose_one'))
    rows = [[InlineKeyboardButton(text=tr(locale, 'back'),
        callback_data='profile_draft_nodes')]]
    if grouped:
        rows[0].append(InlineKeyboardButton(text=tr(locale, 'profile.create.save'),
            callback_data='profile_draft_save'))
    await render(bot, chat_id, Screen(tr(locale, 'profile.create.review_title'),
        tuple(lines)), rows, state, message_id)


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
         tr(locale, 'profile.create.name_prompt'))),
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
    controls = [[InlineKeyboardButton(text=tr(locale, 'profiles.admin.new'),
                callback_data=AccountsCallback().pack())],
            [InlineKeyboardButton(text=tr(locale, 'profiles.admin.search'),
                callback_data='search_profile')]]
    if search:
        controls.append([InlineKeyboardButton(text=tr(locale, 'profiles.admin.show_all'),
            callback_data='admin_profiles_all')])
    rows = [[InlineKeyboardButton(
        text=(tr(locale, 'profile.admin.deleting_prefix') if item.get('deleting') else '')
             + item['display_name'],
        callback_data=AdminProfileCallback(profile_id=item['id']).pack())]
        for item in page['items']]
    rows.extend(controls)
    arrows = []
    if page_index > 0:
        arrows.append(InlineKeyboardButton(text='◀️',
            callback_data=f'admin_profiles_page:{page_index - 1}'))
    if page.get('next_cursor'):
        arrows.append(InlineKeyboardButton(text='▶️',
            callback_data=f'admin_profiles_page:{page_index + 1}'))
    if arrows:
        rows.append(arrows)
    rows.append([InlineKeyboardButton(text=tr(locale, 'back'), callback_data='admin_menu')])
    lines = [tr(locale, 'profiles.admin.description') if page['items'] else
             tr(locale, 'profiles.search.empty' if search else 'profiles.admin.empty')]
    if page_index or page.get('next_cursor'):
        lines.append(tr(locale, 'profiles.admin.page', page=page_index + 1))
    await render(bot, chat_id, Screen(tr(locale, 'profiles.admin.title'), tuple(lines)),
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
        (tr(locale, 'profiles.search.prompt'),)),
        [[InlineKeyboardButton(text=tr(locale, 'cancel'),
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
            (tr(locale, 'profiles.search.invalid'),)),
            [[InlineKeyboardButton(text=tr(locale, 'cancel'),
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
                             state: FSMContext) -> None:
    profile = await backend.request('GET', f'/api/v1/profiles/{profile_id}',
                                    telegram_user_id=user_id)
    grants = (await backend.profile_grants(user_id, profile_id))['items']
    node_page = await backend.admin_nodes(user_id)
    node_names = {node['key']: node['title'] for node in node_page['items']}
    operation = await backend.profile_operation(user_id, profile_id)
    locale = await _locale(state)
    grouped = {}
    for grant in grants:
        grouped.setdefault(grant['node_key'], []).append(tr(locale, f"protocol.{grant['protocol']}"))
    grant_lines = [f"• {node_names.get(key, key)}: {', '.join(protocols)}"
                   for key, protocols in grouped.items()]
    if not grant_lines:
        grant_lines = [tr(locale, 'profile.admin.no_grants')]
    tasks = operation['tasks'] if operation else []
    operation_status = operation['status'] if operation else 'no_targets'
    lines = [tr(locale, 'profile.admin.name', name=profile['display_name']),
             tr(locale, 'profile.admin.id', id=profile['id']),
             tr(locale, 'profile.admin.status',
                status=tr(locale, 'profile.admin.deleting' if profile.get('deleting') else
                    'profile.frozen' if profile['frozen'] else 'profile.active')),
             tr(locale, 'profile.admin.cleanup' if profile.get('deleting') else
                'profile.admin.provision',
                status=tr(locale, f'operation.{operation_status}'),
                ready=sum(task['status'] == 'succeeded' for task in tasks), total=len(tasks)),
             tr(locale, 'profile.admin.grants')]
    if profile.get('deleting') and operation_status == 'blocked':
        lines.append(tr(locale, 'profile.admin.cleanup_blocked'))
    lines.extend(grant_lines[:20])
    rows = []
    if not profile.get('deleting'):
        rows.append([InlineKeyboardButton(text=tr(locale, 'profile.admin.edit'),
            callback_data=f'admin_profile_edit:{profile_id}')])
    rows.extend([[InlineKeyboardButton(text=tr(locale, 'profile.admin.refresh'),
        callback_data=AdminProfileCallback(profile_id=profile_id).pack())],
        [InlineKeyboardButton(text=tr(locale, 'back'),
            callback_data=AdminProfilesCallback().pack())]])
    await render(bot, chat_id, Screen(tr(locale, 'profile.admin.title'), tuple(lines)),
        rows, state, message_id)


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
    profile = await backend.request('GET', f'/api/v1/profiles/{profile_id}',
                                    telegram_user_id=user_id)
    locale = await _locale(state)
    if profile.get('deleting'):
        await show_admin_profile(chat_id, user_id, message_id, profile_id,
                                 bot, backend, state)
        return
    rows = [[InlineKeyboardButton(text=tr(locale, 'profile.admin.rename'),
        callback_data=f'admin_profile_rename:{profile_id}')],
        [InlineKeyboardButton(text=tr(locale, 'profile.admin.protocols'),
        callback_data=GrantNodesCallback(profile_id=profile_id).pack())],
        [InlineKeyboardButton(text=tr(locale, 'profile.admin.status_title'),
        callback_data=f'admin_profile_status:{profile_id}')],
        [InlineKeyboardButton(text=tr(locale, 'profile.admin.delete'),
        callback_data=f'prof_del:{profile_id}')],
        [InlineKeyboardButton(text=tr(locale, 'back'),
        callback_data=AdminProfileCallback(profile_id=profile_id).pack())]]
    await render(bot, chat_id, Screen(tr(locale, 'profile.admin.edit_title'),
        (tr(locale, 'profile.admin.name', name=profile['display_name']),)),
        rows, state, message_id)


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
        callback_data=f'prof_del_go:{profile_id}')],
        [InlineKeyboardButton(text=tr(locale, 'cancel'),
        callback_data=f'admin_profile_edit:{profile_id}')]]
    await render(bot, query.message.chat.id,
        Screen(tr(locale, 'profile.admin.delete_title'),
            (tr(locale, 'profile.admin.name', name=profile['display_name']),
             tr(locale, 'profile.admin.delete_warning'))),
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
        Screen(tr(locale, 'profile.admin.delete_title'), lines),
        rows, state, query.message.message_id)


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
        (tr(locale, 'profile.admin.rename_prompt'),)),
        [[InlineKeyboardButton(text=tr(locale, 'cancel'),
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
            (tr(locale, 'profile.create.invalid_name'),)),
            [[InlineKeyboardButton(text=tr(locale, 'cancel'),
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
            (_profile_error(locale, exc),)),
            [[InlineKeyboardButton(text=tr(locale, 'cancel'),
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
    rows = [[InlineKeyboardButton(
        text=tr(locale, 'profile.admin.unfreeze' if profile['frozen'] else 'profile.admin.freeze'),
        callback_data=ToggleFreezeCallback(profile_id=profile_id).pack())],
        [InlineKeyboardButton(text=tr(locale, 'back'),
        callback_data=f'admin_profile_edit:{profile_id}')]]
    await render(bot, chat_id, Screen(tr(locale, 'profile.admin.status_title'),
        (tr(locale, 'profile.admin.status',
            status=tr(locale, 'profile.frozen' if profile['frozen'] else 'profile.active')),)),
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
    data = await state.get_data()
    if data.get('edit_profile_id') != profile_id:
        page = await backend.profile_grants(user_id, profile_id)
        await state.update_data(edit_profile_id=profile_id,
                                draft_grants=[dict(item) for item in page['items']])
        data = await state.get_data()
    nodes = await backend.admin_nodes(user_id)
    granted = {item['node_key'] for item in data['draft_grants']}
    locale = await _locale(state)
    rows = [[InlineKeyboardButton(
        text=f"{'✅' if node['key'] in granted else '○'} {node['title']}",
        callback_data=GrantProtocolsCallback(profile_id=profile_id,
                                             node_key=node['key']).pack())]
        for node in nodes['items']]
    rows.append([InlineKeyboardButton(text=tr(locale, 'profile.admin.save'),
        callback_data=f'admin_profile_grants_save:{profile_id}')])
    rows.append([InlineKeyboardButton(text=tr(locale, 'back'),
        callback_data=f'admin_profile_edit:{profile_id}')])
    await render(bot, chat_id, Screen(tr(locale, 'profile.admin.nodes_title'),
        (tr(locale, 'profile.admin.nodes_prompt'),)), rows, state, message_id)


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
    data = await state.get_data()
    if data.get('edit_profile_id') != profile_id:
        page = await backend.profile_grants(user_id, profile_id)
        await state.update_data(edit_profile_id=profile_id,
                                draft_grants=[dict(item) for item in page['items']])
        data = await state.get_data()
    enabled = {item['protocol'] for item in data['draft_grants'] if item['node_key'] == node_key}
    locale = await _locale(state)
    node = await backend.request('GET', f'/api/v1/nodes/{node_key}',
                                 telegram_user_id=user_id)
    rows = []
    for protocol in node['protocols']:
        callback = (RemoveGrantCallback if protocol in enabled else AddGrantCallback)(
            profile_id=profile_id, node_key=node_key, protocol=protocol)
        rows.append([InlineKeyboardButton(
            text=f"{'✅' if protocol in enabled else '○'} {protocol.upper()}",
            callback_data=callback.pack())])
    rows.append([InlineKeyboardButton(text=tr(locale, 'back'),
        callback_data=GrantNodesCallback(profile_id=profile_id).pack())])
    await render(bot, chat_id, Screen(tr(locale, 'profile.admin.protocols_title'),
        (tr(locale, 'profile.admin.server', name=node['title']),
         tr(locale, 'profile.admin.protocols_prompt'))),
        rows, state, message_id)


async def change_grant(query: CallbackQuery, profile_id: str, node_key: str,
                       protocol: str, add: bool, bot: Bot,
                       backend: BackendClient, state: FSMContext) -> None:
    user_id = query.from_user.id
    data = await state.get_data()
    if data.get('edit_profile_id') != profile_id:
        page = await backend.profile_grants(user_id, profile_id)
        grants = [dict(item) for item in page['items']]
    else:
        grants = list(data['draft_grants'])
    target = {'node_key': node_key, 'protocol': protocol}
    if add and target not in grants:
        grants.append(target)
    elif not add:
        grants = [item for item in grants if item != target]
    await state.update_data(edit_profile_id=profile_id, draft_grants=grants)
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
    profile = await backend.request('GET', f'/api/v1/profiles/{profile_id}',
                                    telegram_user_id=query.from_user.id)
    try:
        await backend.replace_grants(query.from_user.id, profile_id,
                                     profile['desired_revision'], data['draft_grants'])
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
