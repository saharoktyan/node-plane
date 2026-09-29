import asyncio
from io import BytesIO
from aiogram import Router, Bot, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.filters import CommandStart
import qrcode
from qrcode.exceptions import DataOverflowError

from ..backend import BackendClient
from ..screens import Screen
from .common import render
from .callbacks import (
    HomeCallback, ProfilesCallback, ProfileCallback, NodeCallback,
    IssueCallback, IssuanceStatusCallback, ShowQrCallback,
    RequestAccessCallback, RequestsCallback, AdminProfilesCallback,
    AccountsCallback, AdminNodesCallback, UpdatesCallback, AdminSettingsCallback
)

router = Router()

def get_home_keyboard(account: dict, pending: bool):
    if account['status'] != 'approved':
        return [] if pending else [[InlineKeyboardButton(text='Request access', callback_data=RequestAccessCallback().pack())]]
    
    rows = [[InlineKeyboardButton(text='My profiles', callback_data=ProfilesCallback().pack())]]
    if account['role'] == 'admin':
        rows.append([InlineKeyboardButton(text='Access requests', callback_data=RequestsCallback().pack())])
        rows.append([InlineKeyboardButton(text='Profiles', callback_data=AdminProfilesCallback().pack()),
                     InlineKeyboardButton(text='Accounts', callback_data=AccountsCallback().pack())])
        rows.append([InlineKeyboardButton(text='Nodes', callback_data=AdminNodesCallback().pack()),
                     InlineKeyboardButton(text='Settings', callback_data=AdminSettingsCallback().pack())])
        rows.append([InlineKeyboardButton(text='Updates', callback_data=UpdatesCallback().pack())])
    return rows

@router.message(CommandStart())
async def start_cmd(message: Message, bot: Bot, backend: BackendClient, state: FSMContext):
    if message.from_user is None or message.chat.type != 'private':
        return
    try:
        await backend.resolve(message.from_user.id)
        await show_home(message.chat.id, message.from_user.id, bot, backend, state)
    except Exception:
        await render(bot, message.chat.id, Screen('Node Plane', ('The service is temporarily unavailable. Try /start again.',)), [], state)

@router.callback_query(HomeCallback.filter())
async def home_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    if query.message:
        await show_home(query.message.chat.id, query.from_user.id, bot, backend, state, query.message.message_id)

async def show_home(chat_id: int, user_id: int, bot: Bot, backend: BackendClient, state: FSMContext, message_id: int | None = None):
    account = await backend.me(user_id)
    if account['status'] != 'approved':
        own = await backend.request('GET', '/api/v1/me/access-requests?limit=25', telegram_user_id=user_id)
        pending = any(item['status'] == 'pending' for item in own['items'])
        lines = ('Your access request is waiting for approval.',) if pending else ('Request access to receive VPN configurations.',)
        rows = get_home_keyboard(account, pending)
        await render(bot, chat_id, Screen('Node Plane', lines), rows, state, message_id)
        return
    
    rows = get_home_keyboard(account, False)
    await render(bot, chat_id, Screen('Node Plane', ('Choose what to manage.',)), rows, state, message_id)

@router.callback_query(ProfilesCallback.filter())
async def profiles_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    page = await backend.profiles(query.from_user.id)
    rows = [[InlineKeyboardButton(text=p['display_name'], callback_data=ProfileCallback(profile_id=p['id']).pack())] for p in page['items']]
    rows.append([InlineKeyboardButton(text='Back', callback_data=HomeCallback().pack())])
    await render(bot, query.message.chat.id, Screen('My profiles', 
        ('Select a profile to get a current configuration.',) if page['items'] else ('No profiles are assigned yet.',)), rows, state, query.message.message_id)

@router.callback_query(ProfileCallback.filter())
async def profile_cb(query: CallbackQuery, callback_data: ProfileCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    profile_id = callback_data.profile_id
    user_id = query.from_user.id
    
    profile = await backend.request('GET', f'/api/v1/profiles/{profile_id}', telegram_user_id=user_id)
    nodes = await backend.profile_nodes(user_id, profile_id)
    
    rows = [[InlineKeyboardButton(text=f"{node['flag']} {node['title']}".strip(), 
                                  callback_data=NodeCallback(profile_id=profile_id, node_key=node['key']).pack())] for node in nodes['items']]
    rows.append([InlineKeyboardButton(text='Back', callback_data=ProfilesCallback().pack())])
    status = 'Frozen' if profile['frozen'] else 'Active'
    await render(bot, query.message.chat.id, Screen(profile['display_name'], 
        (f'Status: {status}', 'Select a node.') if nodes['items'] else (f'Status: {status}', 'No nodes are available for this profile.')), rows, state, query.message.message_id)

@router.callback_query(NodeCallback.filter())
async def node_cb(query: CallbackQuery, callback_data: NodeCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    profile_id = callback_data.profile_id
    node_key = callback_data.node_key
    user_id = query.from_user.id
    
    nodes = await backend.profile_nodes(user_id, profile_id)
    node = next((item for item in nodes['items'] if item['key'] == node_key), None)
    if node is None:
        await show_home(query.message.chat.id, user_id, bot, backend, state, query.message.message_id)
        return
        
    rows = []
    for protocol in node['protocols']:
        for transport in protocol['transports']:
            rows.append([InlineKeyboardButton(text=f"{protocol['kind'].upper()} · {transport.upper()}", 
                callback_data=IssueCallback(profile_id=profile_id, node_key=node_key, protocol=protocol['kind'], transport=transport).pack())])
    rows.append([InlineKeyboardButton(text='Back', callback_data=ProfileCallback(profile_id=profile_id).pack())])
    await render(bot, query.message.chat.id, Screen(node['title'], (f"Region: {node['region']}", 'Choose a configuration format.')), rows, state, query.message.message_id)

@router.callback_query(IssueCallback.filter())
async def issue_cb(query: CallbackQuery, callback_data: IssueCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    profile_id, node_key, protocol, transport = callback_data.profile_id, callback_data.node_key, callback_data.protocol, callback_data.transport
    
    queued = await backend.issue(user_id, profile_id, node_key, protocol, transport)
    await render(bot, query.message.chat.id, Screen('Preparing configuration', ('Checking your access and the live node…',)), 
                 [[InlineKeyboardButton(text='Back', callback_data=NodeCallback(profile_id=profile_id, node_key=node_key).pack())]], state, query.message.message_id)
                 
    for _ in range(30):
        s = await backend.issuance(user_id, queued['id'])
        if s['status'] == 'succeeded':
            await show_issuance_status(query.message.chat.id, user_id, query.message.message_id, queued['id'], profile_id, node_key, protocol, transport, bot, backend, state)
            return
        if s['status'] in {'blocked', 'superseded'}:
            break
        await asyncio.sleep(1)
    await show_issuance_status(query.message.chat.id, user_id, query.message.message_id, queued['id'], profile_id, node_key, protocol, transport, bot, backend, state)

@router.callback_query(IssuanceStatusCallback.filter())
async def issuance_status_cb(query: CallbackQuery, callback_data: IssuanceStatusCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await show_issuance_status(query.message.chat.id, query.from_user.id, query.message.message_id, 
                               callback_data.issuance_id, callback_data.profile_id, callback_data.node_key, 
                               callback_data.protocol, callback_data.transport, bot, backend, state)

async def show_issuance_status(chat_id, user_id, message_id, issuance_id, profile_id, node_key, protocol, transport, bot, backend, state):
    s = await backend.issuance(user_id, issuance_id)
    rows = [[InlineKeyboardButton(text='Back', callback_data=NodeCallback(profile_id=profile_id, node_key=node_key).pack())]]
    if s['status'] == 'succeeded':
        artifact = await backend.artifact(user_id, issuance_id)
        filename = artifact['filename'] or f'{protocol}-{node_key}-{transport}.txt'
        await bot.send_document(chat_id, BufferedInputFile(artifact['content'].encode(), filename=filename))
        details = (artifact['content'],) if protocol == 'xray' else ()
        if len(artifact['content'].encode()) <= 2500:
            rows.insert(0, [InlineKeyboardButton(text='Show QR code', callback_data=ShowQrCallback(issuance_id=issuance_id, profile_id=profile_id, node_key=node_key).pack())])
        await render(bot, chat_id, Screen('Configuration ready', ('Import the attached file into your VPN client.',), 'VLESS link' if details else None, details), rows, state, message_id)
        return
    if s['status'] in {'blocked', 'superseded'}:
        await render(bot, chat_id, Screen('Configuration unavailable', ('Check your access and the node status, then request a new config.',)), rows, state, message_id)
        return
    rows.insert(0, [InlineKeyboardButton(text='Refresh', callback_data=IssuanceStatusCallback(issuance_id=issuance_id, profile_id=profile_id, node_key=node_key, protocol=protocol, transport=transport).pack())])
    await render(bot, chat_id, Screen('Still preparing configuration', ('The backend is working. Refresh to check the same request.',)), rows, state, message_id)

@router.callback_query(ShowQrCallback.filter())
async def show_qr_cb(query: CallbackQuery, callback_data: ShowQrCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    user_id, chat_id, message_id = query.from_user.id, query.message.chat.id, query.message.message_id
    issuance_id, profile_id, node_key = callback_data.issuance_id, callback_data.profile_id, callback_data.node_key
    
    artifact = await backend.artifact(user_id, issuance_id)
    content = artifact['content']
    
    rows = [[InlineKeyboardButton(text='Back', callback_data=NodeCallback(profile_id=profile_id, node_key=node_key).pack())]]
    
    if len(content.encode()) > 2500:
        await render(bot, chat_id, Screen('QR unavailable', ('This configuration is too large for a reliable QR code.', 'Use the attached file instead.')), rows, state, message_id)
        return
        
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=6, border=4)
    code.add_data(content)
    try:
        code.make(fit=True)
    except DataOverflowError:
        await render(bot, chat_id, Screen('QR unavailable', ('This configuration is too large for a QR code.',)), rows, state, message_id)
        return
        
    image = BytesIO()
    code.make_image(fill_color='black', back_color='white').save(image, format='PNG')
    await bot.send_photo(chat_id, BufferedInputFile(image.getvalue(), 'config.png'))
    await render(bot, chat_id, Screen('QR code ready', ('Scan the image sent below this menu.',)), rows, state, message_id)

@router.callback_query(RequestAccessCallback.filter())
async def request_access_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    request = await backend.request_access(user_id)
    await show_home(query.message.chat.id, user_id, bot, backend, state, query.message.message_id)
    from .admin_requests import notify_admins
    await notify_admins(bot, backend, request['id'])
