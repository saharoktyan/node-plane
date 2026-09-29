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

