# app/handlers/admin_common.py
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

from i18n import get_locale_for_update, t
from ui.menu import is_admin


def guard(update: Update) -> bool:
    if not is_admin(update):
        update.effective_message.reply_text(t(get_locale_for_update(update), "admin.access_denied"))
        return False
    return True


def kb_back_menu(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "menu.back"), callback_data="menu:main")]])
