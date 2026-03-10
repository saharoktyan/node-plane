# app/ui/menu.py
from __future__ import annotations

from telegram import Update
from telegram.ext import CallbackContext

from config import PARSE_MODE
from i18n import get_locale_for_update, t
from services.app_settings import get_menu_title_markdown
from utils.tg import safe_edit_message
from utils.keyboards import kb_main_menu
import re

def is_admin(update: Update) -> bool:
    from config import ADMIN_IDS
    uid = update.effective_user.id if update.effective_user else None
    return bool(uid) and (uid in ADMIN_IDS)


def render_main_menu(update: Update, context: CallbackContext) -> None:
    lang = get_locale_for_update(update)
    from handlers.user_common import _has_access
    safe_edit_message(
        update,
        context,
        f"*{get_menu_title_markdown()}*\n\n{t(lang, 'menu.choose_action')}",
        reply_markup=kb_main_menu(is_admin(update), _has_access(update), lang),
        parse_mode=PARSE_MODE,
    )

def extract_vpn_key(text: str) -> str | None:
    m = re.search(r"(vpn://\S+)", text or "")
    return m.group(1) if m else None
