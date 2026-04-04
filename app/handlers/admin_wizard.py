# app/handlers/admin_wizard.py
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext

from config import CB_CFG, PARSE_MODE
from domain.servers import (
    get_access_method,
    get_access_methods,
    get_access_methods_for_codes,
    get_access_methods_for_kind,
    get_awg_access_method_by_server_key,
    get_protocol_label,
)
from services import xray as xray_svc
from services.awg import _extract_wg_conf, create_awg_user, delete_awg_user
from services.awg_profiles import get_awg_servers, remove_awg_profile, remove_awg_server, upsert_awg_server
from services.provisioning_state import delete_profile_server_state, upsert_profile_server_state
from services.server_registry import list_servers
from services.profile_state import awg_profile_store, ensure_xray_caps, freeze_profile, is_frozen, profile_store, unfreeze_profile, utcnow
from services.updates import get_updates_menu_emoji, get_updates_overview
from ui.admin_views import (
    render_delete_confirm,
    render_edit_menu,
    render_profile_card,
    render_profile_dashboard,
    render_pick,
    render_proto_keyboard,
    render_proto_server_keyboard,
    render_protocol_server_select_text,
    render_protocol_select_text,
    render_status_menu,
)
from ui.menu import is_admin
from utils.security import validate_profile_name
from utils.keyboards import kb_admin_menu, kb_main_menu
from utils.tg import answer_cb, safe_delete_by_id, safe_delete_update_message, safe_edit_by_ids, safe_edit_message
from i18n import get_locale_for_update, t
from services.app_settings import get_menu_title_markdown

from .admin_common import guard, kb_back_menu


_render_pick = render_pick
_render_proto_keyboard = render_proto_keyboard
_render_proto_server_keyboard = render_proto_server_keyboard
_render_edit_menu = render_edit_menu
_render_delete_confirm = render_delete_confirm
_render_profile_dashboard = render_profile_dashboard
_render_profile_card = render_profile_card
_render_status_menu = render_status_menu


def _wizard_get(context: CallbackContext) -> Optional[Dict[str, Any]]:
    w = context.user_data.get("cfg_wizard")
    return w if isinstance(w, dict) else None


def _wizard_set(context: CallbackContext, w: Dict[str, Any]) -> None:
    context.user_data["cfg_wizard"] = w


def _wizard_clear(context: CallbackContext) -> None:
    context.user_data.pop("cfg_wizard", None)


def _wizard_edit(context: CallbackContext, text: str, markup: InlineKeyboardMarkup) -> None:
    w = _wizard_get(context)
    if not w:
        return
    safe_edit_by_ids(context.bot, int(w["chat_id"]), int(w["message_id"]), text, markup, parse_mode=PARSE_MODE)


def _wizard_edit_plain(context: CallbackContext, text: str, markup: InlineKeyboardMarkup) -> None:
    w = _wizard_get(context)
    if not w:
        return
    safe_edit_by_ids(context.bot, int(w["chat_id"]), int(w["message_id"]), text, markup, parse_mode=None)


def _wizard_init(sent_message, mode: str) -> Dict[str, Any]:
    return {
        "active": True,
        "mode": mode,
        "step": "name" if mode == "create" else "pick",
        "chat_id": sent_message.chat_id,
        "message_id": sent_message.message_id,
        "name": None,
        "protocols": set(),
        "pick_page": 0,
        "all_names": [],
        "dirty": False,
        "locale": "ru",
        "proto_server": None,
    }


def _wizard_lang(context: CallbackContext) -> str:
    w = _wizard_get(context)
    return str(w.get("locale") or "ru") if w else "ru"


def _admin_updates_menu_label(lang: str) -> str:
    overview = get_updates_overview()
    base = t(lang, "menu.updates_plain")
    return f"{get_updates_menu_emoji(overview)} {base}"


def _wizard_close(context: CallbackContext, text: str | None = None) -> None:
    w = _wizard_get(context)
    if not w:
        return
    deleted = safe_delete_by_id(context.bot, int(w["chat_id"]), int(w["message_id"]))
    if not deleted and text:
        safe_edit_by_ids(
            context.bot,
            int(w["chat_id"]),
            int(w["message_id"]),
            text,
            reply_markup=kb_back_menu(str(w.get("locale") or "ru")),
            parse_mode=None,
        )
    elif not deleted:
        try:
            context.bot.edit_message_reply_markup(chat_id=int(w["chat_id"]), message_id=int(w["message_id"]), reply_markup=None)
        except Exception:
            pass
    _wizard_clear(context)


def _start_progress_animation(context: CallbackContext, title: str) -> callable:
    w = _wizard_get(context)
    if not w:
        return lambda: None

    chat_id = int(w["chat_id"])
    message_id = int(w["message_id"])
    lang = _wizard_lang(context)
    text = t(lang, "admin.wizard.work_in_progress", title=title, dots="...")
    safe_edit_by_ids(context.bot, chat_id, message_id, text, InlineKeyboardMarkup([]), parse_mode=None)
    return lambda: None

def _get_all_names() -> List[str]:
    subs = profile_store.read()
    wg = awg_profile_store.read()
    return sorted(set([n for n in subs.keys() if not n.startswith("_")] + list(wg.keys())))


def createcfg_cmd(update: Update, context: CallbackContext) -> None:
    if not guard(update):
        return
    lang = get_locale_for_update(update)
    sent = update.effective_message.reply_text(
        t(lang, "admin.wizard.create_title"),
        parse_mode=PARSE_MODE,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "menu.back"), callback_data=f"{CB_CFG}back")]]),
    )
    w = _wizard_init(sent, "create")
    w["locale"] = lang
    _wizard_set(context, w)
    safe_delete_update_message(update, context)


def changecfg_cmd(update: Update, context: CallbackContext) -> None:
    if not guard(update):
        return
    lang = get_locale_for_update(update)
    sent = update.effective_message.reply_text(t(lang, "admin.wizard.edit_loading"), parse_mode=PARSE_MODE)
    w = _wizard_init(sent, "edit")
    w["locale"] = lang
    w["all_names"] = _get_all_names()
    _wizard_set(context, w)
    _wizard_edit(context, *_render_profile_dashboard(w["all_names"], w["pick_page"], lang))
    safe_delete_update_message(update, context)


def _try_delete_user_msg(update: Update, context: CallbackContext) -> None:
    try:
        msg = update.effective_message
        if msg:
            context.bot.delete_message(chat_id=msg.chat_id, message_id=msg.message_id)
    except Exception:
        pass


def cfg_wizard_text(update: Update, context: CallbackContext) -> None:
    if not is_admin(update):
        return
    w = _wizard_get(context)
    if not w or not w.get("active"):
        return

    txt = (update.effective_message.text or "").strip()
    lang = _wizard_lang(context)
    if w["step"] == "name":
        try:
            name = validate_profile_name(txt)
        except ValueError as exc:
            _wizard_edit(
                context,
                str(exc) if txt else t(lang, "admin.wizard.name_empty"),
                InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "menu.back"), callback_data=f"{CB_CFG}back")]]),
            )
            safe_delete_update_message(update, context)
            return
        w["name"] = name
        w["protocols"] = set()
        w["step"] = "proto"
        w["proto_server"] = None
        _wizard_set(context, w)
        _wizard_edit(context, render_protocol_select_text(name, w["protocols"], lang=lang), _render_proto_keyboard(w["protocols"], lang))
        safe_delete_update_message(update, context)
        return

    if w["step"] == "search":
        q = txt.lstrip("@").strip().lower()
        _try_delete_user_msg(update, context)
        names = w.get("all_names") or _get_all_names()
        w["all_names"] = names
        matches = [name for name in names if q in name.lower()] if q else []
        if not matches:
            _wizard_edit(
                context,
                t(lang, "admin.wizard.search_empty", query=q),
                InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton(t(lang, "menu.back"), callback_data=f"{CB_CFG}back")],
                    ]
                ),
            )
            return

        matches = matches[:30]
        rows = [[InlineKeyboardButton(f"👤 {name}", callback_data=f"{CB_CFG}card:{name}")] for name in matches]
        rows.append([InlineKeyboardButton(t(lang, "menu.back"), callback_data=f"{CB_CFG}back")])
        _wizard_edit(
            context,
            t(lang, "admin.wizard.search_results", count=len(matches), query=q),
            InlineKeyboardMarkup(rows),
        )


def _load_existing(name: str) -> Tuple[Set[str], Optional[int]]:
    rec = profile_store.read().get(name)
    protocols: Set[str] = set()
    if isinstance(rec, dict):
        plist = rec.get("protocols")
        if isinstance(plist, list):
            protocols = {str(code) for code in plist if get_access_method(str(code))}
    for server_key in get_awg_servers(name).keys():
        method = get_awg_access_method_by_server_key(server_key)
        if method:
            protocols.add(method.code)
    return protocols


def _load_profile_into_wizard(context: CallbackContext, name: str) -> Optional[Dict[str, Any]]:
    w = _wizard_get(context)
    if not w:
        return None
    protocols = _load_existing(name)
    w["name"] = name
    w["protocols"] = protocols
    w["all_names"] = w.get("all_names") or _get_all_names()
    _wizard_set(context, w)
    return w


def _run_async_create(context: CallbackContext) -> None:
    w = _wizard_get(context)
    if not w:
        return
    try:
        _finish_create(context)
    except Exception as exc:
        safe_edit_by_ids(
            context.bot,
            int(w["chat_id"]),
            int(w["message_id"]),
            t(_wizard_lang(context), "admin.wizard.create_failed", error=exc),
            reply_markup=kb_back_menu(_wizard_lang(context)),
            parse_mode=PARSE_MODE,
        )


def _run_async_save(context: CallbackContext) -> None:
    w = _wizard_get(context)
    if not w:
        return
    try:
        _save_edit(context)
    except Exception as exc:
        safe_edit_by_ids(
            context.bot,
            int(w["chat_id"]),
            int(w["message_id"]),
            t(_wizard_lang(context), "admin.wizard.save_failed", error=exc),
            reply_markup=kb_back_menu(_wizard_lang(context)),
            parse_mode=PARSE_MODE,
        )


def _resolve_awg_server_keys(protocols: Set[str]) -> List[str]:
    awg_methods = [method for method in get_access_methods_for_codes(protocols) if method.protocol_kind == "awg"]
    return [method.server_key for method in awg_methods]


def on_cfg_callback(update: Update, context: CallbackContext, payload: str) -> None:
    answer_cb(update)
    lang = get_locale_for_update(update)

    if payload.startswith("quickedit:"):
        name = payload.split(":", 1)[1]
        msg = update.callback_query.message
        w = _wizard_init(msg, "edit")
        w["locale"] = lang
        w["all_names"] = _get_all_names()
        _wizard_set(context, w)
        loaded = _load_profile_into_wizard(context, name)
        if not loaded:
            safe_edit_by_ids(
                context.bot,
                msg.chat_id,
                msg.message_id,
                t(lang, "admin.requests.profile_missing"),
                reply_markup=kb_back_menu(lang),
                parse_mode=PARSE_MODE,
            )
            return
        loaded["step"] = "edit_menu"
        _wizard_set(context, loaded)
        safe_edit_by_ids(
            context.bot,
            msg.chat_id,
            msg.message_id,
            *_render_edit_menu(name, loaded["protocols"], frozen=is_frozen(name), lang=lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload in ("start:create", "start:edit"):
        msg = update.callback_query.message
        if payload == "start:create":
            safe_edit_by_ids(
                context.bot,
                msg.chat_id,
                msg.message_id,
                t(lang, "admin.wizard.create_title"),
                InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "menu.back"), callback_data=f"{CB_CFG}back")]]),
                parse_mode=PARSE_MODE,
            )
            w = _wizard_init(msg, "create")
            w["locale"] = lang
            _wizard_set(context, w)
            return

        w = _wizard_init(msg, "edit")
        w["locale"] = lang
        w["all_names"] = _get_all_names()
        _wizard_set(context, w)
        safe_edit_by_ids(context.bot, msg.chat_id, msg.message_id, *_render_profile_dashboard(w["all_names"], w["pick_page"], lang), parse_mode=PARSE_MODE)
        return

    w = _wizard_get(context)
    if not w or not w.get("active"):
        safe_edit_message(
            update,
            context,
            f"*{get_menu_title_markdown()}*\n\n{t(lang, 'menu.choose_action')}",
            reply_markup=kb_main_menu(is_admin(update), True, lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "cancel":
        _wizard_clear(context)
        if update.callback_query and update.callback_query.message:
            safe_edit_message(
                update,
                context,
                f"{t(lang, 'admin.menu_title')}\n\n{t(lang, 'menu.admin_choose')}",
                reply_markup=kb_admin_menu(lang, updates_label=_admin_updates_menu_label(lang)),
                parse_mode=PARSE_MODE,
            )
        return

    if payload == "search":
        w["step"] = "search"
        _wizard_set(context, w)
        _wizard_edit(
            context,
            t(lang, "admin.wizard.search_title"),
            InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton(t(lang, "menu.back"), callback_data=f"{CB_CFG}back")],
                ]
            ),
        )
        return

    if payload == "back":
        if w["step"] == "proto_server":
            w["step"] = "proto"
            w["proto_server"] = None
            _wizard_set(context, w)
            _wizard_edit(context, render_protocol_select_text(w["name"], w["protocols"], editing=w["mode"] == "edit", lang=lang), _render_proto_keyboard(w["protocols"], lang, editing=w["mode"] == "edit"))
            return
        if w["mode"] == "create":
            if w["step"] == "name":
                w["all_names"] = _get_all_names()
                w["pick_page"] = 0
                w["mode"] = "edit"
                w["step"] = "pick"
                _wizard_set(context, w)
                _wizard_edit(context, *_render_profile_dashboard(w["all_names"], w["pick_page"], lang))
                return
            if w["step"] == "proto":
                w["step"] = "name"
                w["proto_server"] = None
                _wizard_set(context, w)
                _wizard_edit(
                    context,
                    t(lang, "admin.wizard.enter_name"),
                    InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "menu.back"), callback_data=f"{CB_CFG}back")]]),
                )
                return
        else:
            if w["step"] == "edit_menu":
                w["step"] = "pick"
                _wizard_set(context, w)
                _wizard_edit(context, *_render_profile_dashboard(w["all_names"], w["pick_page"], lang))
                return
            if w["step"] in ("proto", "delete_confirm", "status_menu"):
                w["step"] = "edit_menu"
                _wizard_set(context, w)
                _wizard_edit(context, *_render_edit_menu(w["name"], w["protocols"], frozen=is_frozen(w["name"]), lang=lang))
                return
            if w["step"] == "search":
                w["step"] = "pick"
                _wizard_set(context, w)
                _wizard_edit(context, *_render_profile_dashboard(w["all_names"], w["pick_page"], lang))
                return
        return

    if payload.startswith("proto:"):
        act = payload.split(":", 1)[1]
        if act == "done":
            if not w["protocols"]:
                _wizard_edit(context, "Нужно выбрать хотя бы один протокол." if lang == "ru" else "You need to choose at least one protocol.", _render_proto_keyboard(w["protocols"], lang))
                return
            _wizard_set(context, w)
            if w["mode"] == "edit":
                context.dispatcher.run_async(_run_async_save, context=context)
            else:
                context.dispatcher.run_async(_run_async_create, context=context)
            return
        if act == "servers":
            w["step"] = "proto"
            w["proto_server"] = None
            _wizard_set(context, w)
            _wizard_edit(
                context,
                render_protocol_select_text(w["name"], w["protocols"], editing=w["mode"] == "edit", lang=lang),
                _render_proto_keyboard(w["protocols"], lang, editing=w["mode"] == "edit"),
            )
            return
        if act.startswith("server:"):
            server_key = act.split(":", 1)[1]
            methods = [method for method in get_access_methods() if method.server_key == server_key]
            if not methods:
                return
            if len(methods) == 1:
                code = methods[0].code
                sel: Set[str] = w["protocols"]
                if code in sel:
                    sel.remove(code)
                else:
                    sel.add(code)
                w["protocols"] = sel
                w["step"] = "proto"
                w["proto_server"] = None
                _wizard_set(context, w)
                _wizard_edit(
                    context,
                    render_protocol_select_text(w["name"], sel, editing=w["mode"] == "edit", lang=lang),
                    _render_proto_keyboard(sel, lang, editing=w["mode"] == "edit"),
                )
                return
            w["step"] = "proto_server"
            w["proto_server"] = server_key
            _wizard_set(context, w)
            _wizard_edit(
                context,
                render_protocol_server_select_text(w["name"], server_key, w["protocols"], editing=w["mode"] == "edit", lang=lang),
                _render_proto_server_keyboard(server_key, w["protocols"], lang),
            )
            return
        if act.startswith("method:"):
            code = act.split(":", 1)[1]
            method = get_access_method(code)
            if not method:
                return
            sel: Set[str] = w["protocols"]
            if code in sel:
                sel.remove(code)
            else:
                sel.add(code)
            w["protocols"] = sel
            w["step"] = "proto_server"
            w["proto_server"] = method.server_key
            _wizard_set(context, w)
            _wizard_edit(
                context,
                render_protocol_server_select_text(w["name"], method.server_key, sel, editing=w["mode"] == "edit", lang=lang),
                _render_proto_server_keyboard(method.server_key, sel, lang),
            )
            return
        if get_access_method(act):
            sel: Set[str] = w["protocols"]
            if act in sel:
                sel.remove(act)
            else:
                sel.add(act)
            w["protocols"] = sel
            _wizard_set(context, w)
            _wizard_edit(
                context,
                render_protocol_select_text(w["name"], sel, editing=w["mode"] == "edit", lang=lang),
                _render_proto_keyboard(sel, lang, editing=w["mode"] == "edit"),
            )
            return

    if payload.startswith("pickpage:"):
        w["pick_page"] = int(payload.split(":", 1)[1])
        _wizard_set(context, w)
        _wizard_edit(context, *_render_pick(w["all_names"], w["pick_page"], lang))
        return

    if payload.startswith("dashboard:"):
        w["pick_page"] = int(payload.split(":", 1)[1])
        _wizard_set(context, w)
        _wizard_edit(context, *_render_profile_dashboard(w["all_names"], w["pick_page"], lang))
        return

    if payload.startswith("card:"):
        name = payload.split(":", 1)[1]
        if not _load_profile_into_wizard(context, name):
            return
        _wizard_edit(context, *_render_profile_card(name, w["protocols"], frozen=is_frozen(name), lang=lang))
        return

    if payload.startswith("cardedit:"):
        name = payload.split(":", 1)[1]
        if not _load_profile_into_wizard(context, name):
            return
        w["step"] = "edit_menu"
        _wizard_set(context, w)
        _wizard_edit(context, *_render_edit_menu(name, w["protocols"], frozen=is_frozen(name), lang=lang))
        return

    if payload.startswith("pick:"):
        name = payload.split(":", 1)[1]
        if not _load_profile_into_wizard(context, name):
            return
        w["step"] = "edit_menu"
        _wizard_set(context, w)
        _wizard_edit(context, *_render_edit_menu(name, w["protocols"], frozen=is_frozen(name), lang=lang))
        return

    if payload.startswith("edit:"):
        act = payload.split(":", 1)[1]
        name = w["name"]
        if act == "proto":
            w["step"] = "proto"
            _wizard_set(context, w)
            _wizard_edit(context, render_protocol_select_text(name, w["protocols"], editing=True, lang=lang), _render_proto_keyboard(w["protocols"], lang, editing=True))
            return
        if act == "status":
            w["step"] = "status_menu"
            _wizard_set(context, w)
            _wizard_edit(context, *_render_status_menu(name, frozen=is_frozen(name), lang=lang))
            return
        if act == "freeze":
            freeze_profile(name)
            w["step"] = "status_menu"
            _wizard_set(context, w)
            _wizard_edit(context, *_render_status_menu(name, frozen=True, lang=lang))
            return
        if act == "unfreeze":
            unfreeze_profile(name)
            w["step"] = "status_menu"
            _wizard_set(context, w)
            _wizard_edit(context, *_render_status_menu(name, frozen=False, lang=lang))
            return
        if act == "save":
            context.dispatcher.run_async(_run_async_save, context=context)
            return
        if act == "delete":
            w["step"] = "delete_confirm"
            _wizard_set(context, w)
            _wizard_edit(context, *_render_delete_confirm(name, lang))
            return
        if act == "delete_confirm":
            _delete_profile_everywhere(context)


def _delete_profile_everywhere(context: CallbackContext) -> None:
    w = _wizard_get(context)
    if not w:
        return
    lang = _wizard_lang(context)
    name = w["name"]
    errors: List[str] = []
    done: List[str] = []

    for server in list_servers():
        if "xray" not in server.protocol_kinds:
            continue
        code, _out = xray_svc.delete_user(name, server.key)
        if code == 0:
            done.append(
                f"Xray {server.key}: "
                + ("удалено" if lang == "ru" else "deleted")
            )
        else:
            errors.append(f"Xray {server.key}: rc={code}")

    awg_servers = get_awg_servers(name)
    for server_key in sorted(awg_servers.keys()):
        code2, _out2 = delete_awg_user(server_key, name)
        if code2 == 0:
            done.append(
                f"AWG {server_key}: "
                + ("удалено" if lang == "ru" else "deleted")
            )
        else:
            errors.append(f"AWG {server_key}: rc={code2}")
    remove_awg_profile(name)

    subs = profile_store.read()
    if isinstance(subs, dict):
        subs.pop(name, None)
        profile_store.write(subs)
    done.append(("состояние профиля: очищено" if lang == "ru" else "profile state: cleared"))

    names = [item for item in _get_all_names() if item != name]
    text = t(lang, "admin.wizard.profile_deleted") + "\n\n" + "\n".join(f"- {item}" for item in done)
    if errors:
        text += (
            "\n\n"
            + ("Ошибки и предупреждения:\n" if lang == "ru" else "Errors and warnings:\n")
            + "\n".join(f"• {err}" for err in errors)
        )
    w["all_names"] = names
    w["pick_page"] = 0
    w["step"] = "pick"
    _wizard_set(context, w)
    _wizard_edit_plain(
        context,
        text + "\n\n" + t(lang, "admin.wizard.next_profile_hint"),
        _render_profile_dashboard(names, 0, lang)[1] if names else kb_back_menu(lang),
    )
    if not names:
        _wizard_clear(context)


def _finish_create(context: CallbackContext) -> None:
    w = _wizard_get(context)
    if not w:
        return
    lang = _wizard_lang(context)
    stop_progress = _start_progress_animation(context, t(_wizard_lang(context), "admin.wizard.profile_create"))
    name: str = w["name"]
    protocols: Set[str] = w["protocols"]

    logging.getLogger("admin").info("CFG create: name=%s protocols=%s", name, sorted(protocols))

    msgs: List[str] = []
    errors: List[str] = []
    xray_state_updates: List[tuple[str, str, Optional[str], Optional[str]]] = []
    uuid_val: Optional[str] = None
    xray_short_id: Optional[str] = None
    subs = profile_store.read()
    rec = subs.get(name, {}) if isinstance(subs.get(name, {}), dict) else {}
    xray_methods = [method for method in get_access_methods_for_codes(protocols) if method.protocol_kind == "xray"]
    existing_xray = rec.get("xray") if isinstance(rec.get("xray"), dict) else {}
    server_short_ids = dict(existing_xray.get("server_short_ids") or {}) if isinstance(existing_xray, dict) else {}
    for method in xray_methods:
        code, out, ensured_uuid, ensured_short_id = xray_svc.ensure_user(name, method.server_key, uuid_value=uuid_val)
        if code != 0 or not ensured_uuid:
            xray_state_updates.append(("failed", method.server_key, uuid_val, (out or "")[-500:] or "create failed"))
            errors.append(f"{method.label}: {t(lang, 'admin.wizard.create_error_line')}\n{(out or '')[-500:]}")
            continue
        uuid_val = ensured_uuid
        if ensured_short_id:
            xray_short_id = ensured_short_id
            server_short_ids[method.server_key] = ensured_short_id
        ready, reason = xray_svc.get_server_link_status(method.server_key)
        if ready:
            xray_state_updates.append(("provisioned", method.server_key, ensured_uuid, None))
            msgs.append(f"{method.label}: {t(lang, 'admin.wizard.synced_line')}")
        else:
            xray_state_updates.append(("needs_attention", method.server_key, ensured_uuid, reason))
            errors.append(f"{method.label}: {t(lang, 'admin.wizard.link_not_ready_line')}\n{reason}")
    if uuid_val:
        ensure_xray_caps(name, uuid_val)

    rec = subs.get(name, {}) if isinstance(subs.get(name, {}), dict) else {}
    now = utcnow()
    rec.update({"type": "none", "created_at": now.isoformat(timespec="minutes"), "expires_at": None, "frozen": bool(rec.get("frozen", False))})

    rec["protocols"] = sorted(protocols)
    if uuid_val:
        rec["uuid"] = uuid_val
        rec["xray"] = {
            "enabled": True,
            "transports": ["xhttp", "tcp"],
            "default": "xhttp",
            "short_id": xray_short_id or "",
            "server_short_ids": server_short_ids,
        }
    subs[name] = rec
    profile_store.write(subs)
    for status, server_key, remote_id, last_error in xray_state_updates:
        upsert_profile_server_state(
            name,
            server_key,
            "xray",
            status=status,
            remote_id=remote_id,
            last_error=last_error,
        )

    awg_methods = [method for method in get_access_methods_for_codes(protocols) if method.protocol_kind == "awg"]
    for awg_method in awg_methods:
        code, cfg, raw = create_awg_user(awg_method.server_key, name)
        if code != 0 or not (cfg or "").strip():
            upsert_profile_server_state(
                name,
                awg_method.server_key,
                "awg",
                status="failed",
                last_error=(raw or "")[-500:] or f"rc={code}",
            )
            errors.append(f"{awg_method.label}: rc={code}\n{(raw or '')[-500:]}")
            continue
        upsert_awg_server(
            name=name,
            server_key=awg_method.server_key,
            config=cfg,
            wg_conf=_extract_wg_conf(cfg),
            created_at=utcnow().isoformat(timespec="minutes"),
        )
        upsert_profile_server_state(name, awg_method.server_key, "awg", status="provisioned", last_error=None)
        msgs.append(f"{awg_method.label}: {t(lang, 'admin.wizard.created_line')}")

    lines = [t(lang, "admin.wizard.profile_created_updated", name=name)] + [f"- {msg}" for msg in msgs]
    if errors:
        lines.append(f"\n{t(lang, 'admin.wizard.errors')}")
        lines.extend(f"• {err}" for err in errors)

    names = _get_all_names()
    w["all_names"] = names
    w["pick_page"] = 0
    w["step"] = "pick"
    _wizard_set(context, w)
    stop_progress()
    _wizard_edit_plain(
        context,
        "\n".join(lines) + "\n\n" + t(lang, "admin.wizard.next_profile_hint"),
        _render_profile_dashboard(names, 0, _wizard_lang(context))[1] if names else kb_back_menu(_wizard_lang(context)),
    )


def _save_edit(context: CallbackContext) -> None:
    w = _wizard_get(context)
    if not w:
        return
    lang = _wizard_lang(context)
    stop_progress = _start_progress_animation(context, t(_wizard_lang(context), "admin.wizard.save"))
    name: str = w["name"]
    protocols: Set[str] = w["protocols"]
    messages: List[str] = []
    errors: List[str] = []

    subs = profile_store.read()
    rec = subs.get(name, {}) if isinstance(subs.get(name, {}), dict) else {}
    existing_protocols = set()
    raw_existing_protocols = rec.get("protocols")
    if isinstance(raw_existing_protocols, list):
        existing_protocols = {str(code) for code in raw_existing_protocols if get_access_method(str(code))}

    now = utcnow()
    rec.update({"type": "none", "expires_at": None, "created_at": rec.get("created_at") or now.isoformat(timespec="minutes")})

    rec["protocols"] = sorted(protocols)

    selected_xray_methods = [method for method in get_access_methods_for_codes(protocols) if method.protocol_kind == "xray"]
    existing_xray_methods = [method for method in get_access_methods_for_codes(existing_protocols) if method.protocol_kind == "xray"]
    selected_xray_server_keys = {method.server_key for method in selected_xray_methods}
    existing_xray_server_keys = {method.server_key for method in existing_xray_methods}

    uuid_val = rec.get("uuid") if isinstance(rec, dict) else None
    xray_short_id = None
    existing_xray = rec.get("xray") if isinstance(rec.get("xray"), dict) else {}
    server_short_ids = dict(existing_xray.get("server_short_ids") or {}) if isinstance(existing_xray, dict) else {}
    for method in selected_xray_methods:
        code, out, ensured_uuid, ensured_short_id = xray_svc.ensure_user(name, method.server_key, uuid_value=uuid_val)
        if code != 0 or not ensured_uuid:
            upsert_profile_server_state(
                name,
                method.server_key,
                "xray",
                status="failed",
                remote_id=uuid_val,
                last_error=(out or "")[-500:] or "sync failed",
            )
            errors.append(f"{method.label}: {t(lang, 'admin.wizard.sync_failed_line')}\n{(out or '')[-500:]}")
            continue
        uuid_val = ensured_uuid
        if ensured_short_id:
            xray_short_id = ensured_short_id
            server_short_ids[method.server_key] = ensured_short_id
        ready, reason = xray_svc.get_server_link_status(method.server_key)
        if ready:
            upsert_profile_server_state(
                name,
                method.server_key,
                "xray",
                status="provisioned",
                remote_id=ensured_uuid,
                last_error=None,
            )
            messages.append(f"{method.label}: {t(lang, 'admin.wizard.synced_line')}")
        else:
            upsert_profile_server_state(
                name,
                method.server_key,
                "xray",
                status="needs_attention",
                remote_id=ensured_uuid,
                last_error=reason,
            )
            errors.append(f"{method.label}: {t(lang, 'admin.wizard.link_not_ready_line')}\n{reason}")

    if uuid_val:
        for removed_server_key in existing_xray_server_keys - selected_xray_server_keys:
            server_short_ids.pop(removed_server_key, None)
        rec["uuid"] = uuid_val
        rec["xray"] = {
            "enabled": True,
            "transports": ["xhttp", "tcp"],
            "default": "xhttp",
            "short_id": xray_short_id or xray_svc.get_short_id_local(name) or "",
            "server_short_ids": server_short_ids,
        }
        ensure_xray_caps(name, uuid_val)

    for server_key in sorted(existing_xray_server_keys - selected_xray_server_keys):
        code, _out = xray_svc.delete_user(name, server_key)
        if code != 0:
            upsert_profile_server_state(
                name,
                server_key,
                "xray",
                status="failed",
                remote_id=uuid_val,
                last_error="delete failed",
            )
            errors.append(f"Xray {server_key}: {t(lang, 'admin.wizard.delete_failed_line')}")
        else:
            delete_profile_server_state(name, server_key, "xray")
            messages.append(f"Xray {server_key}: {t(lang, 'admin.wizard.deleted_line')}")

    subs[name] = rec
    profile_store.write(subs)

    selected_awg_methods = [method for method in get_access_methods_for_codes(protocols) if method.protocol_kind == "awg"]
    selected_server_keys = {method.server_key for method in selected_awg_methods}
    existing_servers = get_awg_servers(name)
    existing_server_keys = set(existing_servers.keys())

    for method in selected_awg_methods:
        if method.server_key in existing_server_keys:
            continue
        code, cfg, raw = create_awg_user(method.server_key, name)
        if code != 0 or not (cfg or "").strip():
            upsert_profile_server_state(
                name,
                method.server_key,
                "awg",
                status="failed",
                last_error=(raw or "")[-500:] or f"rc={code}",
            )
            errors.append(f"{method.label}: rc={code}\n{(raw or '')[-500:]}")
            continue
        upsert_awg_server(
            name=name,
            server_key=method.server_key,
            config=cfg,
            wg_conf=_extract_wg_conf(cfg),
            created_at=utcnow().isoformat(timespec="minutes"),
        )
        upsert_profile_server_state(name, method.server_key, "awg", status="provisioned", last_error=None)
        messages.append(f"{method.label}: {t(lang, 'admin.wizard.created_line')}")

    for server_key in sorted(existing_server_keys - selected_server_keys):
        code, _out = delete_awg_user(server_key, name)
        if code != 0:
            upsert_profile_server_state(
                name,
                server_key,
                "awg",
                status="failed",
                last_error="delete failed",
            )
            errors.append(f"AWG {server_key}: {t(lang, 'admin.wizard.delete_failed_line')}")
            continue
        remove_awg_server(name, server_key)
        delete_profile_server_state(name, server_key, "awg")
        messages.append(f"AWG {server_key}: {t(lang, 'admin.wizard.deleted_line')}")

    text, markup = _render_edit_menu(name, protocols, frozen=is_frozen(name), lang=lang)
    prefix = t(lang, "admin.wizard.profile_saved")
    if messages:
        prefix += "\n" + "\n".join(f"- {msg}" for msg in messages)
    if errors:
        plain_text = (
            prefix
            + f"\n\n{t(lang, 'admin.wizard.errors')}\n"
            + "\n".join(f"• {err}" for err in errors)
            + "\n\n"
            + text.replace("*", "").replace("`", "")
        )
        stop_progress()
        _wizard_edit_plain(context, plain_text, markup)
        return
    stop_progress()
    _wizard_edit(context, prefix + "\n\n" + text, markup)
