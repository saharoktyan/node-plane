from aiogram import Router, Bot, F
from aiogram.types import CallbackQuery, InlineKeyboardButton
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext

from .common import render, BackendMiddleware
from ..backend import BackendClient, BackendError
from ..screens import Screen
from .callbacks import AdminSettingsCallback, HomeCallback, UpdatesCallback

class SshKeyCallback(CallbackData, prefix="ssh_key"):
    pass

class RegenerateSshKeyCallback(CallbackData, prefix="regen_ssh"):
    pass

router = Router()

@router.callback_query(AdminSettingsCallback.filter())
async def admin_settings_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    rows = [
        [
            InlineKeyboardButton(text="Название бота", callback_data="settings_bot_title"),
            InlineKeyboardButton(text="Заявки на доступ", callback_data="settings_requests"),
        ],
        [
            InlineKeyboardButton(text="Обновления", callback_data=UpdatesCallback().pack()),
            InlineKeyboardButton(text="💾 Бэкапы", callback_data="settings_backups"),
        ],
        [InlineKeyboardButton(text="🔐 SSH ключ", callback_data=SshKeyCallback().pack())],
        [InlineKeyboardButton(text="Сброс / Очистка", callback_data="settings_cleanup")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_menu")]
    ]
    await render(bot, query.message.chat.id, Screen("Настройки системы", ("Глобальные параметры контроллера.",)), rows, state, query.message.message_id)


@router.callback_query(SshKeyCallback.filter())
async def ssh_key_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    try:
        response = await backend.request("GET", "/api/v1/system/ssh-key", telegram_user_id=query.from_user.id)
        pub_key = response["public_key"]
        
        rows = [
            [InlineKeyboardButton(text="🔙 Назад", callback_data=AdminSettingsCallback().pack())]
        ]
        
        await render(bot, query.message.chat.id, Screen(
            "Управление SSH ключом",
            ("Этот публичный ключ используется для аутентификации при автоматической установке узлов через SSH.", "Добавьте этот ключ в ~/.ssh/authorized_keys на целевом VPS перед добавлением узла в Node Plane."),
            "Публичный ключ",
            (f"`{pub_key}`",)
        ), rows, state, query.message.message_id)
        
    except BackendError as exc:
        await render(bot, query.message.chat.id, Screen("Ошибка", (f"Не удалось прочитать SSH ключ: {exc.code}",)), [[InlineKeyboardButton(text="🔙 Назад", callback_data=AdminSettingsCallback().pack())]], state, query.message.message_id)

@router.callback_query(F.data.startswith("settings_"))
async def settings_placeholder_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    rows = [[InlineKeyboardButton(text="🔙 Назад", callback_data=AdminSettingsCallback().pack())]]
    await render(bot, query.message.chat.id, Screen("В разработке", ("Этот раздел настроек еще не перенесен.",)), rows, state, query.message.message_id)

