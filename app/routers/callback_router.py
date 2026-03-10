# app/routers/callback_router.py
from __future__ import annotations

import logging
from telegram import Update
from telegram.ext import CallbackContext
from utils.tg import answer_cb, safe_edit_message
from utils.keyboards import kb_main_menu
from config import CB_CFG, CB_GETKEY, CB_MENU, CB_SRV, PARSE_MODE
from handlers import user as user_handlers
from handlers import admin as admin_handlers
from i18n import get_locale_for_update, t
from services.app_settings import get_menu_title_markdown

logger = logging.getLogger(__name__)


def on_callback(update: Update, context: CallbackContext) -> None:
    answer_cb(update)

    q = update.callback_query
    if not q:
        return
    data = q.data or ""

    # menu
    if data.startswith(CB_MENU):
        payload = data[len(CB_MENU):]
        user_handlers.on_menu_callback(update, context, payload)
        return

    # getkey
    if data.startswith(CB_GETKEY):
        payload = data[len(CB_GETKEY):]
        user_handlers.on_getkey_callback(update, context, payload)
        return

    # cfg wizard (admin)
    if data.startswith(CB_CFG):
        payload = data[len(CB_CFG):]
        admin_handlers.on_cfg_callback(update, context, payload)
        return

    if data.startswith(CB_SRV):
        payload = data[len(CB_SRV):]
        admin_handlers.on_server_callback(update, context, payload)
        return

    safe_edit_message(
        update, context,
        f"*{get_menu_title_markdown()}*\n\n{t(get_locale_for_update(update), 'menu.choose_action')}",
        reply_markup=kb_main_menu(
            user_handlers._is_admin(update),
            user_handlers._has_access(update),
            get_locale_for_update(update),
        ),
        # если kb_main_menu не импортируется — см. ниже
        parse_mode=PARSE_MODE,
    )
