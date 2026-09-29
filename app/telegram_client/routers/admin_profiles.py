from aiogram import Router, Bot, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.fsm.context import FSMContext

from ..backend import BackendClient, BackendError
from ..screens import Screen
from .common import render
from .states import ProfileDraftState
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
    rows.append([InlineKeyboardButton(text='Back', callback_data=HomeCallback().pack())])
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
        [InlineKeyboardButton(text='Back', callback_data=AccountsCallback().pack())]
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
    user_id = query.from_user.id
    page = await backend.admin_profiles(user_id)
    rows = [[InlineKeyboardButton(text=item['display_name'], callback_data=AdminProfileCallback(profile_id=item['id']).pack())] for item in page['items']]
    rows.append([InlineKeyboardButton(text='Back', callback_data=HomeCallback().pack())])
    await render(bot, query.message.chat.id, Screen('VPN profiles', ('Select a profile to manage access.',) if page['items'] else ('No profiles yet. Create one from Accounts.',)), rows, state, query.message.message_id)

@router.callback_query(AdminProfileCallback.filter())
async def admin_profile_cb(query: CallbackQuery, callback_data: AdminProfileCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await show_admin_profile(query.message.chat.id, query.from_user.id, query.message.message_id, callback_data.profile_id, bot, backend, state)

async def show_admin_profile(chat_id: int, user_id: int, message_id: int, profile_id: str, bot: Bot, backend: BackendClient, state: FSMContext):
    profile = await backend.request('GET', f'/api/v1/profiles/{profile_id}', telegram_user_id=user_id)
    grants = (await backend.profile_grants(user_id, profile_id))['items']
    status = 'Frozen' if profile['frozen'] else 'Active'
    
    rows = [
        [InlineKeyboardButton(text='Unfreeze' if profile['frozen'] else 'Freeze', callback_data=ToggleFreezeCallback(profile_id=profile_id).pack())],
        [InlineKeyboardButton(text='Add access', callback_data=GrantNodesCallback(profile_id=profile_id).pack())]
    ]
    for grant in grants:
        rows.append([InlineKeyboardButton(text=f"Remove {grant['node_key']} · {grant['protocol'].upper()}", callback_data=RemoveGrantCallback(profile_id=profile_id, node_key=grant['node_key'], protocol=grant['protocol']).pack())])
    rows.append([InlineKeyboardButton(text='Back', callback_data=AdminProfilesCallback().pack())])
    
    lines = (f'Status: {status}', f'Access entries: {len(grants)}', 'Changes are applied by the backend worker.')
    await render(bot, chat_id, Screen(profile['display_name'], lines), rows, state, message_id)

@router.callback_query(GrantNodesCallback.filter())
async def grant_nodes_cb(query: CallbackQuery, callback_data: GrantNodesCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    profile_id = callback_data.profile_id
    page = await backend.admin_nodes(user_id)
    rows = [[InlineKeyboardButton(text=node['title'], callback_data=GrantProtocolsCallback(profile_id=profile_id, node_key=node['key']).pack())] for node in page['items'] if node['enabled']]
    rows.append([InlineKeyboardButton(text='Back', callback_data=AdminProfileCallback(profile_id=profile_id).pack())])
    await render(bot, query.message.chat.id, Screen('Choose a node', ('Only installed and enabled nodes can receive profile access.',)), rows, state, query.message.message_id)

@router.callback_query(GrantProtocolsCallback.filter())
async def grant_protocols_cb(query: CallbackQuery, callback_data: GrantProtocolsCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    profile_id = callback_data.profile_id
    node_key = callback_data.node_key
    
    node = await backend.request('GET', f'/api/v1/nodes/{node_key}', telegram_user_id=user_id)
    rows = [[InlineKeyboardButton(text=protocol.upper(), callback_data=AddGrantCallback(profile_id=profile_id, node_key=node_key, protocol=protocol).pack())] for protocol in node['protocols']]
    rows.append([InlineKeyboardButton(text='Back', callback_data=GrantNodesCallback(profile_id=profile_id).pack())])
    await render(bot, query.message.chat.id, Screen(node['title'], ('Choose a protocol to grant.',)), rows, state, query.message.message_id)

@router.callback_query(AddGrantCallback.filter())
async def add_grant_cb(query: CallbackQuery, callback_data: AddGrantCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await change_grant(query.from_user.id, callback_data.profile_id, callback_data.node_key, callback_data.protocol, True, backend)
    await show_admin_profile(query.message.chat.id, query.from_user.id, query.message.message_id, callback_data.profile_id, bot, backend, state)

@router.callback_query(RemoveGrantCallback.filter())
async def remove_grant_cb(query: CallbackQuery, callback_data: RemoveGrantCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await change_grant(query.from_user.id, callback_data.profile_id, callback_data.node_key, callback_data.protocol, False, backend)
    await show_admin_profile(query.message.chat.id, query.from_user.id, query.message.message_id, callback_data.profile_id, bot, backend, state)

async def change_grant(user_id: int, profile_id: str, node_key: str, protocol: str, add: bool, backend: BackendClient):
    profile = await backend.request('GET', f'/api/v1/profiles/{profile_id}', telegram_user_id=user_id)
    grants = (await backend.profile_grants(user_id, profile_id))['items']
    target = {'node_key': node_key, 'protocol': protocol}
    if add and target not in grants:
        grants.append(target)
    elif not add and target in grants:
        grants = [g for g in grants if g != target]
    await backend.replace_grants(user_id, profile_id, profile['desired_revision'], grants)

@router.callback_query(ToggleFreezeCallback.filter())
async def toggle_freeze_cb(query: CallbackQuery, callback_data: ToggleFreezeCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    profile_id = callback_data.profile_id
    
    profile = await backend.request('GET', f'/api/v1/profiles/{profile_id}', telegram_user_id=user_id)
    await backend.edit_profile(user_id, profile_id, profile['desired_revision'], {'frozen': not profile['frozen']})
    await show_admin_profile(query.message.chat.id, user_id, query.message.message_id, profile_id, bot, backend, state)

