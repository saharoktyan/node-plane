from aiogram import Router, Bot, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.fsm.context import FSMContext

from ..backend import BackendClient, BackendError
from ..screens import Screen
from .common import render
from .states import ProfileDraftState

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
    
    await state.set_state(ProfileDraftState.waiting_for_name)
    await state.update_data(profile_account_id=account_id)
    
    rows = [[InlineKeyboardButton(text='Cancel', callback_data=AccountsCallback().pack())]]
    await render(bot, query.message.chat.id, Screen('New VPN profile', ('Send the profile name as a message.',)), rows, state, query.message.message_id)

@router.message(ProfileDraftState.waiting_for_name, F.text)
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
    
    # We will assume identity is a TG ID or a username
    tg_id = None
    if identity.isdigit():
        tg_id = int(identity)
    else:
        # If it's a username, we can't easily resolve it to TG ID without them messaging the bot
        # So we just create a profile without an owner, or fail. The prompt said "с выбором по tg id / tg username"
        pass
        
    try:
        account_id = None
        if tg_id:
            # Resolve account
            res = await backend.request('POST', '/api/v1/integrations/telegram/identities/resolve', command=True, body={'telegram_user_id': tg_id})
            account_id = res['id']
            display_name = f"tg_{tg_id}"
        else:
            # Just create a loose profile with the username as display name
            display_name = identity
            
        # Create profile
        await backend.request('POST', '/api/v1/profiles', telegram_user_id=message.from_user.id, command=True, body={
            'display_name': display_name,
            'owner_account_id': account_id
        })
        
        await state.clear()
        rows = [[InlineKeyboardButton(text='🔙 Вернуться', callback_data=AdminProfilesCallback().pack())]]
        await render(bot, message.chat.id, Screen('Создание профиля', (f'Профиль {display_name} успешно создан.',)), rows, state)
        
    except Exception as exc:
        rows = [[InlineKeyboardButton(text='🔙 Назад', callback_data=AdminProfilesCallback().pack())]]
        await render(bot, message.chat.id, Screen('Ошибка', (f'Не удалось создать профиль: {exc}',)), rows, state)



@router.message(ProfileSearchState.waiting_for_query)
async def process_profile_search(message: Message, bot: Bot, backend: BackendClient, state: FSMContext):
    await message.delete()
    query = message.text.strip().lower()
    await state.clear()
    
    try:
        # Fetch profiles (in a real app, this should have a search endpoint or we paginate until we find it)
        # We will fetch up to 100 and filter locally
        res = await backend.request('GET', '/api/v1/profiles?limit=100', telegram_user_id=message.from_user.id)
        items = res.get('items', [])
        
        matches = []
        for p in items:
            # Match ID or display name
            if query in p['id'].lower() or query in p['display_name'].lower():
                matches.append(p)
                
        rows = []
        for match in matches[:10]:
            rows.append([InlineKeyboardButton(text=match['display_name'], callback_data=AdminProfileCallback(profile_id=match['id']).pack())])
            
        rows.append([InlineKeyboardButton(text='🔙 Вернуться', callback_data=AdminProfilesCallback().pack())])
        
        if not matches:
            await render(bot, message.chat.id, Screen('Результаты поиска', (f'По запросу "{query}" ничего не найдено.',)), rows, state)
        else:
            await render(bot, message.chat.id, Screen('Результаты поиска', (f'Найдено профилей: {len(matches)}',)), rows, state)
            
    except Exception as exc:
        rows = [[InlineKeyboardButton(text='🔙 Вернуться', callback_data=AdminProfilesCallback().pack())]]
        await render(bot, message.chat.id, Screen('Ошибка', (f'Ошибка при поиске: {exc}',)), rows, state)


@router.callback_query(AdminProfileCallback.filter())
async def admin_profile_cb(query: CallbackQuery, callback_data: AdminProfileCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await show_admin_profile(query.message.chat.id, query.from_user.id, query.message.message_id, callback_data.profile_id, bot, backend, state)

async def show_admin_profile(chat_id, user_id, message_id, profile_id, bot, backend, state):
    try:
        profile = await backend.request('GET', f'/api/v1/profiles/{profile_id}', telegram_user_id=user_id)
    except BackendError:
        return

    rows = [
        [InlineKeyboardButton(text="🔑 Доступы (Servers)", callback_data=GrantNodesCallback(profile_id=profile_id).pack())],
        [InlineKeyboardButton(text="🧊 " + ("Разморозить" if profile.get('status') == 'frozen' else "Заморозить"), callback_data=ToggleFreezeCallback(profile_id=profile_id).pack())],
        [InlineKeyboardButton(text="🔙 Назад", callback_data=AdminProfilesCallback().pack())]
    ]
    
    lines = [
        f"Профиль: {profile['display_name']}",
        f"ID: {profile['id']}",
        f"Статус: {profile.get('status', 'active')}"
    ]
    await render(bot, chat_id, Screen('Управление профилем', tuple(lines)), rows, state, message_id)

@router.callback_query(GrantNodesCallback.filter())
async def grant_nodes_cb(query: CallbackQuery, callback_data: GrantNodesCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    profile_id = callback_data.profile_id
    user_id = query.from_user.id
    
    # fetch all nodes
    page = await backend.admin_nodes(user_id)
    all_nodes = page['items']
    
    # fetch profile grants
    grants = await backend.request('GET', f'/api/v1/profiles/{profile_id}/grants', telegram_user_id=user_id)
    granted_node_keys = {g['node_key']: g for g in grants}
    
    rows = []
    for node in all_nodes:
        nk = node['key']
        is_granted = nk in granted_node_keys
        btn_text = f"✅ {node['title']}" if is_granted else f"❌ {node['title']}"
        action = RemoveGrantCallback(profile_id=profile_id, node_key=nk).pack() if is_granted else AddGrantCallback(profile_id=profile_id, node_key=nk).pack()
        rows.append([InlineKeyboardButton(text=btn_text, callback_data=action)])
        
        if is_granted:
            rows.append([InlineKeyboardButton(text=f"  ↳ Протоколы ({nk})", callback_data=GrantProtocolsCallback(profile_id=profile_id, node_key=nk).pack())])

    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=AdminProfileCallback(profile_id=profile_id).pack())])
    
    await render(bot, query.message.chat.id, Screen('Доступ к серверам', ('Выберите серверы для выдачи доступа.',)), rows, state, query.message.message_id)

@router.callback_query(AddGrantCallback.filter())
async def add_grant_cb(query: CallbackQuery, callback_data: AddGrantCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    grants = await backend.request('GET', f'/api/v1/profiles/{callback_data.profile_id}/grants', telegram_user_id=user_id)
    new_grants = [{'node_key': g['node_key'], 'protocols': g['protocols']} for g in grants]
    new_grants.append({'node_key': callback_data.node_key, 'protocols': ['xray', 'amneziawg']})profile = await backend.request('GET', f'/api/v1/profiles/{callback_data.profile_id}', telegram_user_id=user_id)
    await backend.request('PATCH', f'/api/v1/profiles/{callback_data.profile_id}/grants', body={'grants': new_grants}, telegram_user_id=user_id, command=True, revision=profile['revision'])
    await grant_nodes_cb(query, GrantNodesCallback(profile_id=callback_data.profile_id), bot, backend, state)

@router.callback_query(RemoveGrantCallback.filter())
async def remove_grant_cb(query: CallbackQuery, callback_data: RemoveGrantCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    grants = await backend.request('GET', f'/api/v1/profiles/{callback_data.profile_id}/grants', telegram_user_id=user_id)
    new_grants = [{'node_key': g['node_key'], 'protocols': g['protocols']} for g in grants if g['node_key'] != callback_data.node_key]profile = await backend.request('GET', f'/api/v1/profiles/{callback_data.profile_id}', telegram_user_id=user_id)
    await backend.request('PATCH', f'/api/v1/profiles/{callback_data.profile_id}/grants', body={'grants': new_grants}, telegram_user_id=user_id, command=True, revision=profile['revision'])
    await grant_nodes_cb(query, GrantNodesCallback(profile_id=callback_data.profile_id), bot, backend, state)


from .callbacks import ToggleProtocolCallback

@router.callback_query(ToggleProtocolCallback.filter())
async def toggle_protocol_cb(query: CallbackQuery, callback_data: ToggleProtocolCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    profile_id = callback_data.profile_id
    node_key = callback_data.node_key
    protocol = callback_data.protocol
    user_id = query.from_user.id
    
    grants = await backend.request('GET', f'/api/v1/profiles/{profile_id}/grants', telegram_user_id=user_id)
    grant = next((g for g in grants if g['node_key'] == node_key), None)
    if not grant:
        await grant_nodes_cb(query, GrantNodesCallback(profile_id=profile_id), bot, backend, state)
        return
        
    protos = grant['protocols']
    new_protos = list(protos)
    if protocol in new_protos: new_protos.remove(protocol)
    else: new_protos.append(protocol)
    
    new_grants = [{'node_key': g['node_key'], 'protocols': g['protocols']} for g in grants if g['node_key'] != node_key]
    new_grants.append({'node_key': node_key, 'protocols': new_protos})profile = await backend.request('GET', f'/api/v1/profiles/{profile_id}', telegram_user_id=user_id)
    await backend.request('PATCH', f'/api/v1/profiles/{profile_id}/grants', body={'grants': new_grants}, telegram_user_id=user_id, command=True, revision=profile['revision'])
    await grant_protocols_cb(query, GrantProtocolsCallback(profile_id=profile_id, node_key=node_key), bot, backend, state)


@router.callback_query(GrantProtocolsCallback.filter())
async def grant_protocols_cb(query: CallbackQuery, callback_data: GrantProtocolsCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    profile_id = callback_data.profile_id
    node_key = callback_data.node_key
    user_id = query.from_user.id
    
    grants = await backend.request('GET', f'/api/v1/profiles/{profile_id}/grants', telegram_user_id=user_id)
    grant = next((g for g in grants if g['node_key'] == node_key), None)
    if not grant:
        await grant_nodes_cb(query, GrantNodesCallback(profile_id=profile_id), bot, backend, state)
        return
        
    protos = grant['protocols']
    
    node = await backend.request('GET', f'/api/v1/nodes/{node_key}', telegram_user_id=user_id)
    node_protocols = node.get('protocols', [])
    
    rows = []
    for np in node_protocols:
        is_selected = np in protos
        label = f">{np.capitalize()}<" if is_selected else np.capitalize()
        rows.append([InlineKeyboardButton(text=label, callback_data=ToggleProtocolCallback(profile_id=profile_id, node_key=node_key, protocol=np).pack())])
        
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=GrantNodesCallback(profile_id=profile_id).pack())])
    await render(bot, query.message.chat.id, Screen('Доступ к протоколам', (f"Выберите протоколы для сервера {node_key}.",)), rows, state, query.message.message_id)

@router.callback_query(ToggleFreezeCallback.filter())
async def toggle_freeze_cb(query: CallbackQuery, callback_data: ToggleFreezeCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    profile_id = callback_data.profile_id
    
    profile = await backend.request('GET', f'/api/v1/profiles/{profile_id}', telegram_user_id=user_id)
    is_frozen = profile.get('status') == 'frozen'
    new_frozen = not is_frozen
    
    await backend.request('PATCH', f'/api/v1/profiles/{profile_id}', body={'frozen': new_frozen}, telegram_user_id=user_id, command=True, revision=profile['revision'])
    await admin_profile_cb(query, AdminProfileCallback(profile_id=profile_id), bot, backend, state)


