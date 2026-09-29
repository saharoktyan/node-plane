from aiogram import Router, Bot, F
from aiogram.types import CallbackQuery, InlineKeyboardButton
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext

from .common import render, BackendMiddleware
from ..backend import BackendClient, BackendError
from ..screens import Screen
from .callbacks import AdminSettingsCallback, HomeCallback

class SshKeyCallback(CallbackData, prefix="ssh_key"):
    pass

class RegenerateSshKeyCallback(CallbackData, prefix="regen_ssh"):
    pass

router = Router()

@router.callback_query(AdminSettingsCallback.filter())
async def admin_settings_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    rows = [
        [InlineKeyboardButton(text="SSH Key Management", callback_data=SshKeyCallback().pack())],
        [InlineKeyboardButton(text="Home", callback_data=HomeCallback().pack())]
    ]
    await render(bot, query.message.chat.id, Screen("System Settings", ("Configure global controller settings.",)), rows, state, query.message.message_id)


@router.callback_query(SshKeyCallback.filter())
async def ssh_key_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    try:
        response = await backend.request("GET", "/api/v1/system/ssh-key", telegram_user_id=query.from_user.id)
        pub_key = response["public_key"]
        
        rows = [
            [InlineKeyboardButton(text="Back", callback_data=AdminSettingsCallback().pack())]
        ]
        
        await render(bot, query.message.chat.id, Screen(
            "SSH Key Management",
            ("This public key is used to authenticate when the bot automatically installs nodes via SSH.", "Add this key to ~/.ssh/authorized_keys on the target VPS before adding it to Node Plane."),
            "Public Key",
            (f"`{pub_key}`",)
        ), rows, state, query.message.message_id)
        
    except BackendError as exc:
        await render(bot, query.message.chat.id, Screen("Error", (f"Failed to read SSH key: {exc.code}",)), [[InlineKeyboardButton(text="Back", callback_data=AdminSettingsCallback().pack())]], state, query.message.message_id)

