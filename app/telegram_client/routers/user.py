from aiogram import Router, Bot, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.filters import CommandStart

from ..backend import BackendClient, BackendError
from ..screens import Screen
from .common import render

from app.services.profile_state import user_store
from app.i18n import t, set_user_locale, get_user_locale
from .callbacks import HomeCallback, RequestAccessCallback, ProfilesCallback, AdminSettingsCallback, RequestsCallback, AdminProfilesCallback, AdminNodesCallback, UpdatesCallback

router = Router()

@router.message(CommandStart())
async def start_cmd(message: Message, bot: Bot, backend: BackendClient, state: FSMContext):
    try: await message.delete()
    except: pass
    if message.from_user is None or message.chat.type != 'private':
        return

    try:
        user_rec = user_store.get_user(message.from_user.id)
        if not user_rec or not user_rec.get('locale_explicitly_selected'):
            rows = [
                [InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_select:ru")],
                [InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_select:en")]
            ]
            await render(bot, message.chat.id, Screen('Language / Язык', ('Select your language / Выберите язык:',)), rows, state)
            return
    except Exception:
        pass
    
    try:
        await backend.resolve(message.from_user.id)
        await show_home(message.chat.id, message.from_user.id, bot, backend, state)
    except Exception as e:
        await render(bot, message.chat.id, Screen('Node Plane', ('Сервис временно недоступен.',)), [], state)

@router.callback_query(HomeCallback.filter())
async def home_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    if query.message:
        await show_home(query.message.chat.id, query.from_user.id, bot, backend, state, query.message.message_id)

async def show_home(chat_id: int, user_id: int, bot: Bot, backend: BackendClient, state: FSMContext, message_id: int | None = None):
    try:
        account = await backend.request("GET", "/api/v1/me", telegram_user_id=user_id)
        is_admin = account.get('role') == 'admin'
        has_access = account.get('status') == 'approved'
    except BackendError:
        is_admin = False
        has_access = False
    
    rows = []
    if not has_access:
        rows.append([InlineKeyboardButton(text="🚀 Запросить доступ", callback_data=RequestAccessCallback().pack())])
    else:
        rows.append([InlineKeyboardButton(text="🔑 Получить ключ", callback_data=ProfilesCallback().pack())])
        rows.append([InlineKeyboardButton(text="👤 Мой профиль", callback_data="user_profile")])
        rows.append([InlineKeyboardButton(text="⚙️ Настройки", callback_data="user_settings")])
        
    if is_admin:
        rows.append([InlineKeyboardButton(text="👑 Админ-панель", callback_data="admin_menu")])
        
    await render(bot, chat_id, Screen('Главное меню', ('Добро пожаловать в Node Plane!',)), rows, state, message_id)

@router.callback_query(F.data == "user_profile")
async def profile_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    rows = [[InlineKeyboardButton(text="🔙 Назад", callback_data=HomeCallback().pack())]]
    await render(bot, query.message.chat.id, Screen('Мой профиль', ('Ваша учетная запись Node Plane.',)), rows, state, query.message.message_id)


@router.callback_query(F.data.startswith("lang_select:"))
async def lang_select_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    lang = query.data.split(":")[1]
    set_user_locale(query.from_user.id, lang)
    if query.message:
        await show_home(query.message.chat.id, query.from_user.id, bot, backend, state, query.message.message_id)

@router.callback_query(F.data == "user_settings")
async def settings_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    lang = get_user_locale(query.from_user.id)
    rows = [
        [InlineKeyboardButton(text="🇷🇺 Русский" if lang == "en" else "🇬🇧 English", callback_data="lang_select:en" if lang == "ru" else "lang_select:ru")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data=HomeCallback().pack())]
    ]
    await render(bot, query.message.chat.id, Screen('Настройки', ('Настройки пользователя.',)), rows, state, query.message.message_id)


@router.callback_query(F.data == "admin_menu")
async def admin_menu_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    rows = [
        [
            InlineKeyboardButton(text="📋 Статус", callback_data="admin_status"),
            InlineKeyboardButton(text="🎫 Заявки", callback_data=RequestsCallback().pack()),
        ],
        [
            InlineKeyboardButton(text="🖥 Серверы", callback_data=AdminNodesCallback().pack()),
            InlineKeyboardButton(text="👥 Профили", callback_data=AdminProfilesCallback().pack()),
        ],
        [
            InlineKeyboardButton(text="📢 Оповещение", callback_data="admin_announce"),
        ],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data=AdminSettingsCallback().pack())],
        [InlineKeyboardButton(text="🔙 Назад", callback_data=HomeCallback().pack())],
    ]
    await render(bot, query.message.chat.id, Screen('Админ-панель', ('Управление Node Plane.',)), rows, state, query.message.message_id)

@router.callback_query(F.data == "admin_status")
async def admin_status_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    rows = [[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_menu")]]
    await render(bot, query.message.chat.id, Screen('Статус', ('В разработке...',)), rows, state, query.message.message_id)

@router.callback_query(F.data == "admin_announce")
async def admin_announce_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    rows = [[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_menu")]]
    await render(bot, query.message.chat.id, Screen('Оповещение', ('В разработке...',)), rows, state, query.message.message_id)



from .callbacks import ProfilesCallback

class NodeSelectCallback(CallbackData, prefix="node_sel"):
    profile_id: str
    node_key: str

class ProtocolSelectCallback(CallbackData, prefix="proto_sel"):
    profile_id: str
    node_key: str
    protocol: str
    transport: str

@router.callback_query(ProfilesCallback.filter())
async def profiles_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    try:
        # Fetch user's profiles
        profiles_res = await backend.request('GET', f'/api/v1/accounts/{user_id}/profiles', telegram_user_id=user_id)
        profiles = profiles_res.get('items', [])
        
        if not profiles:
            rows = [[InlineKeyboardButton(text="🔙 Назад", callback_data=HomeCallback().pack())]]
            await render(bot, query.message.chat.id, Screen("Профили", ("У вас нет активных VPN-профилей.",)), rows, state, query.message.message_id)
            return
            
        # If exactly one profile, auto-select it and show nodes
        if len(profiles) == 1:
            await show_profile_nodes(query.message.chat.id, user_id, profiles[0]['id'], bot, backend, state, query.message.message_id)
            return
            
        rows = []
        for p in profiles:
            rows.append([InlineKeyboardButton(text=p['display_name'], callback_data=f"sel_prof:{p['id']}")])
        rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=HomeCallback().pack())])
        await render(bot, query.message.chat.id, Screen("Выбор профиля", ("У вас несколько профилей. Выберите нужный:",)), rows, state, query.message.message_id)
    except Exception as e:
        await query.answer(f"Error: {e}", show_alert=True)

@router.callback_query(F.data.startswith("sel_prof:"))
async def select_profile_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    profile_id = query.data.split(":")[1]
    await show_profile_nodes(query.message.chat.id, query.from_user.id, profile_id, bot, backend, state, query.message.message_id)

async def show_profile_nodes(chat_id: int, user_id: int, profile_id: str, bot: Bot, backend: BackendClient, state: FSMContext, message_id: int):
    # Fetch grants for the profile to see which nodes are available
    grants_res = await backend.request('GET', f'/api/v1/profiles/{profile_id}/grants', telegram_user_id=user_id)
    grants = grants_res.get('items', [])
    
    # Deduplicate nodes
    node_keys = list(set([g['node_key'] for g in grants]))
    
    if not node_keys:
        rows = [[InlineKeyboardButton(text="🔙 Назад", callback_data=HomeCallback().pack())]]
        await render(bot, chat_id, Screen("Серверы", ("Нет доступных серверов для этого профиля.",)), rows, state, message_id)
        return
        
    # We should get node titles, but for simplicity we'll just use the keys if we can't fetch all at once
    # Better to fetch the nodes page
    nodes_res = await backend.request('GET', '/api/v1/nodes?limit=100', telegram_user_id=user_id)
    nodes = {n['key']: n for n in nodes_res.get('items', [])}
    
    rows = []
    for nk in node_keys:
        node = nodes.get(nk)
        title = node['title'] if node else nk
        flag = node.get('flag', '🏳️') if node else '🏳️'
        rows.append([InlineKeyboardButton(text=f"{flag} {title}", callback_data=NodeSelectCallback(profile_id=profile_id, node_key=nk).pack())])
    
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=HomeCallback().pack())])
    await render(bot, chat_id, Screen("Выбор сервера", ("Выберите сервер для получения ключа:",)), rows, state, message_id)

@router.callback_query(NodeSelectCallback.filter())
async def select_node_cb(query: CallbackQuery, callback_data: NodeSelectCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    pid = callback_data.profile_id
    nk = callback_data.node_key
    
    grants_res = await backend.request('GET', f'/api/v1/profiles/{pid}/grants', telegram_user_id=user_id)
    grants = [g for g in grants_res.get('items', []) if g['node_key'] == nk]
    
    rows = []
    for g in grants:
        proto = g['protocol']
        if proto == 'xray':
            rows.append([InlineKeyboardButton(text="Xray (VLESS/TCP/XTLS)", callback_data=ProtocolSelectCallback(profile_id=pid, node_key=nk, protocol='xray', transport='tcp').pack())])
            # Xray XTLS is the default TCP transport
            # Optional: Add HTTP Upgrade if needed, we'll keep it simple
        elif proto == 'awg':
            rows.append([InlineKeyboardButton(text="AmneziaWG (VPN)", callback_data=ProtocolSelectCallback(profile_id=pid, node_key=nk, protocol='awg', transport='vpn').pack())])
            rows.append([InlineKeyboardButton(text="AmneziaWG (Config)", callback_data=ProtocolSelectCallback(profile_id=pid, node_key=nk, protocol='awg', transport='conf').pack())])
            
    rows.append([InlineKeyboardButton(text="🔙 К списку серверов", callback_data=ProfilesCallback().pack())])
    await render(bot, query.message.chat.id, Screen("Протоколы", ("Выберите протокол для подключения:",)), rows, state, query.message.message_id)

import asyncio

@router.callback_query(ProtocolSelectCallback.filter())
async def select_protocol_cb(query: CallbackQuery, callback_data: ProtocolSelectCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    user_id = query.from_user.id
    pid = callback_data.profile_id
    nk = callback_data.node_key
    proto = callback_data.protocol
    trans = callback_data.transport
    
    try:
        res = await backend.request('POST', f'/api/v1/profiles/{pid}/config-issuances', telegram_user_id=user_id, command=True, json={
            "node_key": nk,
            "protocol": proto,
            "transport": trans
        })
        issuance_id = res['id']
        
        # Poll for completion
        for _ in range(30):
            status_res = await backend.request('GET', f'/api/v1/config-issuances/{issuance_id}', telegram_user_id=user_id)
            if status_res['status'] == 'succeeded':
                break
            if status_res['status'] in ('blocked', 'failed'):
                await query.answer("Не удалось сгенерировать конфиг.", show_alert=True)
                return
            await asyncio.sleep(1)
            
        # Fetch artifact
        art_res = await backend.request('GET', f'/api/v1/config-issuances/{issuance_id}/artifact', telegram_user_id=user_id)
        content = art_res['content']
        
        # Answer query now that we have data
        await query.answer()
        
        # Render
        rows = [[InlineKeyboardButton(text="🔙 К списку серверов", callback_data=ProfilesCallback().pack())]]
        if trans == 'vpn':
            import urllib.parse
            safe_content = urllib.parse.quote(content)
            # Create a mock QR or just send string
            await render(bot, query.message.chat.id, Screen("AmneziaWG", (f"`{content}`",)), rows, state, query.message.message_id)
        elif trans == 'conf':
            # Send document
            import base64
            # Usually conf comes in raw or base64. Let's assume it's raw text for now
            await render(bot, query.message.chat.id, Screen("AmneziaWG Config", (f"`{content}`",)), rows, state, query.message.message_id)
        elif proto == 'xray':
            await render(bot, query.message.chat.id, Screen("Xray URL", (f"`{content}`",)), rows, state, query.message.message_id)
            
    except Exception as e:
        await query.answer(f"Ошибка получения конфига: {e}", show_alert=True)
