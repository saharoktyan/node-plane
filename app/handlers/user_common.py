# app/handlers/user_common.py
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from telegram import Update
from telegram.ext import CallbackContext

from config import ADMIN_IDS, APP_VERSION, PARSE_MODE
from domain.servers import get_access_methods_for_codes, get_tracked_awg_server_keys
from i18n import detect_locale, get_locale_for_update, t
from services.app_settings import are_access_requests_enabled, get_access_gate_message, get_menu_title, get_menu_title_markdown
from services.profile_state import ensure_telegram_profile, get_profile, user_store
from utils.keyboards import kb_language_menu, kb_main_menu


def _touch_key_stat(context: CallbackContext, user_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    user_store.bump_key_stat(user_id, now)


def _parse_iso(dt_str: str):
    try:
        if dt_str.endswith("Z"):
            dt_str = dt_str[:-1] + "+00:00"
        return datetime.fromisoformat(dt_str)
    except Exception:
        return None


def _human_ago(iso: str, lang: str = "ru") -> str:
    dt = _parse_iso(iso)
    if not dt:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    sec = int((now - dt).total_seconds())
    if sec < 0:
        sec = 0

    if sec < 60:
        return f"{sec} сек назад" if lang == "ru" else f"{sec} sec ago"
    if sec < 3600:
        return f"{sec // 60} мин назад" if lang == "ru" else f"{sec // 60} min ago"
    if sec < 86400:
        return f"{sec // 3600} ч назад" if lang == "ru" else f"{sec // 3600} h ago"
    return f"{sec // 86400} дн назад" if lang == "ru" else f"{sec // 86400} d ago"


def _progress_bar(p: float, width: int = 10) -> str:
    p = 0.0 if p < 0 else 1.0 if p > 1 else p
    filled = int(round(p * width))
    filled = max(0, min(width, filled))
    return "▰" * filled + "▱" * (width - filled)


def _sub_progress(created_iso: str, expires_iso: str) -> tuple[str, str]:
    c = _parse_iso(created_iso)
    e = _parse_iso(expires_iso)
    if not c or not e:
        return "—", "—"
    if c.tzinfo is None:
        c = c.replace(tzinfo=timezone.utc)
    if e.tzinfo is None:
        e = e.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    total = (e - c).total_seconds()
    left = (e - now).total_seconds()
    if total <= 0:
        return "—", "—"

    used = total - max(0, left)
    p = used / total
    return _progress_bar(p, 10), f"{int(round(p * 100))}%"


def _human_left(exp_iso: str, lang: str = "ru") -> str:
    dt = _parse_iso(exp_iso)
    if not dt:
        return "—"
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    sec = int((dt - now).total_seconds())
    if sec <= 0:
        return "истекла" if lang == "ru" else "expired"
    days = sec // 86400
    hrs = (sec % 86400) // 3600
    if days > 0:
        return f"{days} дн {hrs} ч" if lang == "ru" else f"{days} d {hrs} h"
    mins = (sec % 3600) // 60
    return f"{hrs} ч {mins} мин" if lang == "ru" else f"{hrs} h {mins} min"


def _conf_msg_key(server_key: str) -> str:
    return f"last_awg_conf_msg_id:{server_key}"


def _delete_last_awg_conf(context: CallbackContext, chat_id: int, server_key: str) -> None:
    key = _conf_msg_key(server_key)
    mid = context.user_data.get(key)
    if not mid:
        return
    try:
        context.bot.delete_message(chat_id=chat_id, message_id=int(mid))
    except Exception:
        pass
    context.user_data.pop(key, None)


def _delete_all_awg_conf(context: CallbackContext, chat_id: int) -> None:
    for server_key in get_tracked_awg_server_keys():
        _delete_last_awg_conf(context, chat_id, server_key)


def _is_admin(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else None
    return bool(uid) and (uid in ADMIN_IDS)


def _has_access(update: Update) -> bool:
    if _is_admin(update):
        return True
    user = update.effective_user
    if not user:
        return False
    db = user_store.read()
    urec = db.get(str(user.id)) if isinstance(db, dict) else None
    return bool(isinstance(urec, dict) and urec.get("access_granted"))


def _build_getkey_items(codes: List[str]) -> List[Tuple[str, str]]:
    return [(method.getkey_payload, method.label) for method in get_access_methods_for_codes(codes)]


def _access_gate_text(user_id: int, lang: str) -> str:
    if not are_access_requests_enabled():
        return get_access_gate_message()
    db = user_store.read()
    rec = db.get(str(user_id)) if isinstance(db, dict) else None
    if isinstance(rec, dict):
        if rec.get("access_request_pending"):
            return t(lang, "access.pending")
        if rec.get("access_request_sent_at") and not rec.get("access_granted"):
            return t(lang, "access.rejected")
    return t(lang, "access.welcome")


def _resolve_profile_name(user_id: int | None) -> str | None:
    if user_id is None:
        return None
    db = user_store.read()
    rec = db.get(str(user_id)) if isinstance(db, dict) else None
    if not isinstance(rec, dict):
        return None
    profile_name = str(rec.get("profile_name") or "").strip()
    if profile_name and get_profile(profile_name):
        return profile_name
    return None


def _build_start_reply(update: Update, lang: str, now_iso: str) -> tuple[str, Any]:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return "", None

    if _is_admin(update):
        profile_name = ensure_telegram_profile(user.id, preferred_name=user.username or "")
        user_store.upsert_user(
            user.id,
            chat_id=chat.id,
            username=user.username or "",
            first_name=user.first_name or "",
            last_name=user.last_name or "",
            profile_name=profile_name,
            locale=lang,
            access_granted=True,
            access_request_pending=False,
            access_request_sent_at=None,
            updated_at=now_iso,
        )
    has_access = _has_access(update)
    if has_access:
        def clear_pending(db: Dict[str, Any]) -> Dict[str, Any]:
            rec = db.get(str(user.id))
            if isinstance(rec, dict):
                rec["access_request_pending"] = False
                db[str(user.id)] = rec
            return db

        user_store.update(clear_pending)
    if not has_access:
        text = _access_gate_text(user.id, lang)
        return (
            text if not are_access_requests_enabled() else f"*{get_menu_title_markdown()}*\n\n{text}",
            kb_main_menu(False, False, lang, allow_requests=are_access_requests_enabled()),
        )
    return (
        f"*{get_menu_title_markdown()}*\n\n{t(lang, 'menu.choose_action')}",
        kb_main_menu(_is_admin(update), has_access, lang),
    )


def start_cmd(update: Update, context: CallbackContext) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    existing_users = user_store.read()
    existing_rec = existing_users.get(str(user.id)) if isinstance(existing_users, dict) else None
    is_first_start = not isinstance(existing_rec, dict)

    def mut(db: Dict[str, Any]) -> Dict[str, Any]:
        rec = db.get(str(user.id))
        if not isinstance(rec, dict):
            rec = {}
        rec.update(
            {
                "chat_id": chat.id,
                "username": user.username or "",
                "first_name": user.first_name or "",
                "last_name": user.last_name or "",
                "profile_name": rec.get("profile_name"),
                "locale": rec.get("locale") or detect_locale(update),
                "updated_at": now_iso,
                "notify_access_requests": bool(rec.get("notify_access_requests", True)),
                "telemetry_enabled": bool(rec.get("telemetry_enabled", False)),
                "access_granted": bool(rec.get("access_granted", False)),
                "access_request_pending": bool(rec.get("access_request_pending", False)),
            }
        )
        db[str(user.id)] = rec
        return db

    user_store.update(mut)
    lang = get_locale_for_update(update)
    if is_first_start:
        if _is_admin(update):
            _build_start_reply(update, lang, now_iso)
        context.user_data["start_language_gate_pending"] = True
        update.effective_message.reply_text(
            t(lang, "language.first_start_title"),
            parse_mode=PARSE_MODE,
            reply_markup=kb_language_menu(lang, include_back=False, show_selected=False, callback_action="setlangstart"),
            disable_web_page_preview=True,
        )
        return
    text, markup = _build_start_reply(update, lang, now_iso)
    if not text:
        return
    update.effective_message.reply_text(text, parse_mode=PARSE_MODE, reply_markup=markup, disable_web_page_preview=True)


def whoami_cmd(update: Update, context: CallbackContext) -> None:
    user = update.effective_user
    if not user:
        return
    update.effective_message.reply_text(f"Your id: {user.id}\nusername: @{user.username or ''}")


def version_cmd(update: Update, context: CallbackContext) -> None:
    lang = get_locale_for_update(update)
    update.effective_message.reply_text(f"{get_menu_title()}\n{t(lang, 'version.label', version=APP_VERSION)}")


def getkey_cmd(update: Update, context: CallbackContext) -> None:
    start_cmd(update, context)
