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
            res = await backend.request('POST', '/api/v1/integrations/telegram/identities/resolve', command=True, json={'telegram_user_id': tg_id})
            account_id = res['id']
            display_name = f"tg_{tg_id}"
        else:
            # Just create a loose profile with the username as display name
            display_name = identity
            
        # Create profile
        await backend.request('POST', '/api/v1/profiles', telegram_user_id=message.from_user.id, command=True, json={
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
