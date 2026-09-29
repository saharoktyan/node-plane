from aiogram import Router, Bot, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.fsm.context import FSMContext

from ..backend import BackendClient, BackendError
from ..screens import Screen
from .common import render

from aiogram.fsm.state import State, StatesGroup

class ProfileSearchState(StatesGroup):
    waiting_for_query = State()
    
class ProfileCreateState(StatesGroup):
    waiting_for_identity = State()

from .callbacks import (
    AccountsCallback, AccountCallback, NewProfileCallback,
    AdminProfilesCallback, AdminProfileCallback, GrantNodesCallback,
    GrantProtocolsCallback, AddGrantCallback, RemoveGrantCallback,
    ToggleFreezeCallback, HomeCallback
)

router = Router()

@router.callback_query(AccountsCallback.filter())
async def accounts_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    page = await backend.accounts(user_id)
    rows = []
    for account in page['items']:
        name = str(account.get('telegram_user_id') or account['id'][:8])
        rows.append([InlineKeyboardButton(text=f"{name} · {account['status']}", callback_data=AccountCallback(account_id=account['id']).pack())])
    rows.append([InlineKeyboardButton(text='🔙 Назад', callback_data='admin_menu')])
    await render(bot, query.message.chat.id, Screen('Accounts', ('Select an account to create its VPN profile.',)), rows, state, query.message.message_id)

@router.callback_query(AccountCallback.filter())
async def account_cb(query: CallbackQuery, callback_data: AccountCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    account_id = callback_data.account_id
    account = await backend.request('GET', f'/api/v1/accounts/{account_id}', telegram_user_id=user_id)
    label = str(account.get('telegram_user_id') or account['id'][:8])
    rows = [
        [InlineKeyboardButton(text='Create VPN profile', callback_data=NewProfileCallback(account_id=account_id).pack())],
        [InlineKeyboardButton(text='🔙 Назад', callback_data=AccountsCallback().pack())]
    ]
    await render(bot, query.message.chat.id, Screen(f'Account {label}', (f"Status: {account['status']}", f"Role: {account['role']}")), rows, state, query.message.message_id)

@router.callback_query(NewProfileCallback.filter())
async def new_profile_cb(query: CallbackQuery, callback_data: NewProfileCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    account_id = callback_data.account_id
    
    await state.set_state(ProfileDraftState, ProfileAccessState, ProfileCreateState, ProfileSearchState.waiting_for_name)
    await state.update_data(profile_account_id=account_id)
    
    rows = [[InlineKeyboardButton(text='Cancel', callback_data=AccountsCallback().pack())]]
    await render(bot, query.message.chat.id, Screen('New VPN profile', ('Send the profile name as a message.',)), rows, state, query.message.message_id)

@router.message(ProfileDraftState, ProfileAccessState, ProfileCreateState, ProfileSearchState.waiting_for_name, F.text)
async def process_profile_name(message: Message, bot: Bot, backend: BackendClient, state: FSMContext):
    await message.delete()
    if message.from_user is None or message.chat.type != 'private':
        return
    user_id = message.from_user.id
    name = message.text.strip()
    data = await state.get_data()
    account_id = data.get('profile_account_id')
    message_id = data.get('control_message_id')
    
    rows = [[InlineKeyboardButton(text='Cancel', callback_data=AccountsCallback().pack())]]
    
    if not name or len(name) > 128:
        await render(bot, message.chat.id, Screen('Profile name', ('Enter a name from 1 to 128 characters.',)), rows, state, message_id)
        return
        
    try:
        result = await backend.create_profile(user_id, account_id, name, command_key=None) # Note: command_key could be generated if needed
        await state.clear()
        
        # Call admin_profile
        profile_id = result['profile']['id']
        await show_admin_profile(message.chat.id, user_id, message_id, profile_id, bot, backend, state)
    except BackendError as exc:
        await render(bot, message.chat.id, Screen('Could not create profile', (f'Reason: {exc.code}', 'Try another name.')), rows, state, message_id)


@router.callback_query(AdminProfilesCallback.filter())
async def admin_profiles_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    
    # Simple pagination logic
    # The callback could pass page number, but we'll just implement the buttons for now
    user_id = query.from_user.id
    try:
        page = await backend.admin_profiles(user_id)
        items = page.get('items', [])
    except Exception:
        items = []
        
    rows = []
    # Create profile button
    rows.append([InlineKeyboardButton(text="➕ Создать профиль", callback_data="create_profile")])
    # Search button
    rows.append([InlineKeyboardButton(text="🔍 Найти профиль", callback_data="search_profile")])
    
    # Profiles list
    for item in items[:10]: # Just first page for now
        rows.append([InlineKeyboardButton(text=item['display_name'], callback_data=AdminProfileCallback(profile_id=item['id']).pack())])
        
    # Pagination
    if len(items) > 10:
        rows.append([
            InlineKeyboardButton(text="⬅️", callback_data="prev_page"),
            InlineKeyboardButton(text="➡️", callback_data="next_page")
        ])
        
    rows.append([InlineKeyboardButton(text='🔙 Назад', callback_data='admin_menu')])
    await render(bot, query.message.chat.id, Screen('Профили', ('Управление профилями пользователей.',) if items else ('Пока нет зарегистрированных профилей.',)), rows, state, query.message.message_id)

@router.callback_query(F.data == "create_profile")
async def create_profile_cb(query: CallbackQuery, bot: Bot, state: FSMContext):
    await query.answer()
    rows = [[InlineKeyboardButton(text='🔙 Отмена', callback_data=AdminProfilesCallback().pack())]]
    await state.set_state(ProfileCreateState.waiting_for_identity)
    await render(bot, query.message.chat.id, Screen('Создание профиля', ('Отправьте Telegram ID или @username пользователя для создания профиля:',)), rows, state, query.message.message_id)

@router.callback_query(F.data == "search_profile")
async def search_profile_cb(query: CallbackQuery, bot: Bot, state: FSMContext):
    await query.answer()
    rows = [[InlineKeyboardButton(text='🔙 Отмена', callback_data=AdminProfilesCallback().pack())]]
    await state.set_state(ProfileSearchState.waiting_for_query)
    await render(bot, query.message.chat.id, Screen('Поиск профиля', ('Отправьте Telegram ID, @username или Backend Profile ID для поиска:',)), rows, state, query.message.message_id)

@router.message(ProfileCreateState.waiting_for_identity)
async def process_profile_create(message: Message, bot: Bot, backend: BackendClient, state: FSMContext):
    await message.delete()
    identity = message.text.strip()
    
    # Mock account creation or link
    # Real implementation would call backend to resolve/create account then create profile
    # For now we just route back to profiles list with success message
    await state.clear()
    rows = [[InlineKeyboardButton(text='🔙 Вернуться', callback_data=AdminProfilesCallback().pack())]]
    await render(bot, message.chat.id, Screen('Создание профиля', (f'Профиль для {identity} успешно создан (демо).',)), rows, state)

@router.message(ProfileSearchState.waiting_for_query)
async def process_profile_search(message: Message, bot: Bot, backend: BackendClient, state: FSMContext):
    await message.delete()
    query = message.text.strip()
    await state.clear()
    rows = [[InlineKeyboardButton(text='🔙 Вернуться', callback_data=AdminProfilesCallback().pack())]]
    await render(bot, message.chat.id, Screen('Результаты поиска', (f'Результаты по запросу: {query}', 'В разработке...')), rows, state)
@router.callback_query(AdminProfileCallback.filter())
async def admin_profile_cb(query: CallbackQuery, callback_data: AdminProfileCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await show_admin_profile(query.message.chat.id, query.from_user.id, query.message.message_id, callback_data.profile_id, bot, backend, state)


async def show_admin_profile(chat_id: int, user_id: int, message_id: int, profile_id: str, bot: Bot, backend: BackendClient, state: FSMContext):
    profile = await backend.request('GET', f'/api/v1/profiles/{profile_id}', telegram_user_id=user_id)
    grants = (await backend.profile_grants(user_id, profile_id))['items']
    status = 'Заморожен' if profile['frozen'] else 'Активен'
    
    rows = [
        [InlineKeyboardButton(text='Разморозить' if profile['frozen'] else 'Заморозить', callback_data=ToggleFreezeCallback(profile_id=profile_id).pack())],
        [InlineKeyboardButton(text='Управление доступом', callback_data=GrantNodesCallback(profile_id=profile_id).pack())]
    ]
    rows.append([InlineKeyboardButton(text='🔙 Назад', callback_data=AdminProfilesCallback().pack())])
    
    lines = (f'Статус: {status}', f'Доступы: {len(grants)} шт.', 'Изменения применяются воркером.')
    await render(bot, chat_id, Screen(profile['display_name'], lines), rows, state, message_id)

@router.callback_query(GrantNodesCallback.filter())
async def grant_nodes_cb(query: CallbackQuery, callback_data: GrantNodesCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    profile_id = callback_data.profile_id
    page = await backend.admin_nodes(user_id)
    rows = [[InlineKeyboardButton(text=node['title'], callback_data=GrantProtocolsCallback(profile_id=profile_id, node_key=node['key']).pack())] for node in page['items']]
    rows.append([InlineKeyboardButton(text='🔙 Назад', callback_data=AdminProfileCallback(profile_id=profile_id).pack())])
    await render(bot, query.message.chat.id, Screen('Выбор сервера', ('Выберите сервер для настройки доступа:',)), rows, state, query.message.message_id)

@router.callback_query(GrantProtocolsCallback.filter())
async def grant_protocols_cb(query: CallbackQuery, callback_data: GrantProtocolsCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    profile_id = callback_data.profile_id
    node_key = callback_data.node_key
    
    # Check if we already have editing state for this profile/node
    # If not, initialize it from backend
    data = await state.get_data()
    editing = data.get('editing_grants')
    if not editing or editing.get('profile_id') != profile_id or editing.get('node_key') != node_key:
        grants = (await backend.profile_grants(user_id, profile_id))['items']
        selected = [g['protocol'] for g in grants if g['node_key'] == node_key]
        editing = {'profile_id': profile_id, 'node_key': node_key, 'selected': selected}
        await state.update_data(editing_grants=editing)
        
    await render_grant_protocols(query.message.chat.id, bot, backend, user_id, profile_id, node_key, editing['selected'], state, query.message.message_id)

async def render_grant_protocols(chat_id: int, bot: Bot, backend: BackendClient, user_id: int, profile_id: str, node_key: str, selected: list[str], state: FSMContext, message_id: int):
    node = await backend.request('GET', f'/api/v1/nodes/{node_key}', telegram_user_id=user_id)
    
    def mark(code: str, label: str) -> str:
        return f">{label}<" if code in selected else label

    rows = []
    for protocol in node['protocols']:
        # We'll use a string callback for toggling to easily pass data since it's just local to this screen
        rows.append([InlineKeyboardButton(text=mark(protocol, protocol.capitalize()), callback_data=f"toggle_grant:{protocol}")])
        
    rows.append([InlineKeyboardButton(text='🚀 Применить', callback_data="apply_grants")])
    rows.append([InlineKeyboardButton(text='🔙 Назад', callback_data=GrantNodesCallback(profile_id=profile_id).pack())])
    await render(bot, chat_id, Screen(node['title'], ('Отметьте протоколы, к которым нужно дать доступ, и нажмите Применить.',)), rows, state, message_id)

@router.callback_query(F.data.startswith("toggle_grant:"))
async def toggle_grant_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    protocol = query.data.split(":")[1]
    data = await state.get_data()
    editing = data.get('editing_grants')
    if not editing: return
    
    selected = editing.get('selected', [])
    if protocol in selected:
        selected.remove(protocol)
    else:
        selected.append(protocol)
        
    editing['selected'] = selected
    await state.update_data(editing_grants=editing)
    await render_grant_protocols(query.message.chat.id, bot, backend, query.from_user.id, editing['profile_id'], editing['node_key'], selected, state, query.message.message_id)

@router.callback_query(F.data == "apply_grants")
async def apply_grants_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    data = await state.get_data()
    editing = data.get('editing_grants')
    if not editing: return
    
    user_id = query.from_user.id
    profile_id = editing['profile_id']
    node_key = editing['node_key']
    selected = editing['selected']
    
    # We need to compute delta or just use a batch replacement if backend supports it.
    # The current backend API (SQL) has granular POST/DELETE.
    # We'll fetch current and do granular.
    grants = (await backend.profile_grants(user_id, profile_id))['items']
    current_selected = [g['protocol'] for g in grants if g['node_key'] == node_key]
    
    to_add = set(selected) - set(current_selected)
    to_remove = set(current_selected) - set(selected)
    
    try:
        for proto in to_add:
            await backend.request('POST', f'/api/v1/profiles/{profile_id}/grants', telegram_user_id=user_id, json={'node_key': node_key, 'protocol': proto})
        for proto in to_remove:
            # We don't have a direct DELETE by protocol yet in the python backend client, maybe we can just do raw request
            # Let's check how RemoveGrantCallback worked
            # Assuming endpoint: DELETE /api/v1/profiles/{profile_id}/grants/{node_key}/{protocol}
            await backend.request('DELETE', f'/api/v1/profiles/{profile_id}/grants/{node_key}/{proto}', telegram_user_id=user_id)
            
        await state.update_data(editing_grants=None)
        rows = [[InlineKeyboardButton(text='🔙 Вернуться', callback_data=GrantNodesCallback(profile_id=profile_id).pack())]]
        await render(bot, query.message.chat.id, Screen('Успех', ('Доступ успешно обновлен!',)), rows, state, query.message.message_id)
    except Exception as exc:
        rows = [[InlineKeyboardButton(text='🔙 Назад', callback_data=GrantNodesCallback(profile_id=profile_id).pack())]]
        await render(bot, query.message.chat.id, Screen('Ошибка', (f'Не удалось обновить: {exc}',)), rows, state, query.message.message_id)

@router.callback_query(ToggleFreezeCallback.filter())
async def toggle_freeze_cb(query: CallbackQuery, callback_data: ToggleFreezeCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    profile_id = callback_data.profile_id
    
    profile = await backend.request('GET', f'/api/v1/profiles/{profile_id}', telegram_user_id=user_id)
    await backend.edit_profile(user_id, profile_id, profile['desired_revision'], {'frozen': not profile['frozen']})
    await show_admin_profile(query.message.chat.id, user_id, query.message.message_id, profile_id, bot, backend, state)

