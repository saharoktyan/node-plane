# app/handlers/user_getkey.py
from __future__ import annotations

import io
from collections import defaultdict
from typing import Dict, List

import qrcode
from telegram import Update
from telegram.ext import CallbackContext

from config import PARSE_MODE
from domain.servers import AccessMethod, get_access_method_by_getkey_payload, get_access_methods_for_codes, get_awg_access_method_by_server_key, get_server
from i18n import get_locale_for_update, t
from services.app_settings import get_menu_title_markdown
from services import xray as xray_svc
from services.awg import _extract_wg_conf
from services.awg_profiles import get_awg_server, update_awg_server
from services.profile_state import _extract_vpn_key, ensure_xray_caps, get_allowed_protocols, get_profile, get_profile_access_status
from ui.user_views import render_getkey_overview, render_server_menu
from utils.keyboards import (
    kb_awg_key_actions,
    kb_getkey_attachment_back,
    kb_back_to_getkey_menu,
    kb_back_to_main,
    kb_getkey_server_methods,
    kb_getkey_servers,
    kb_main_menu,
    kb_xray_key_actions,
    kb_xray_transport,
)
from utils.tg import answer_cb, safe_delete_update_message, safe_edit_message

from .user_common import (
    _conf_msg_key,
    _delete_all_awg_conf,
    _delete_last_awg_conf,
    _is_admin,
    _resolve_profile_name,
    _touch_key_stat,
)

def _group_methods_by_server(codes: List[str]) -> Dict[str, List[AccessMethod]]:
    grouped: Dict[str, List[AccessMethod]] = defaultdict(list)
    for method in get_access_methods_for_codes(codes):
        grouped[method.server_key].append(method)
    return dict(grouped)


def _server_back_payload(server_key: str) -> str:
    return f"getkey:server:{server_key}"


def _build_qr_bytes(data: str) -> io.BytesIO:
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def _artifact_msg_key(kind: str, token: str) -> str:
    return f"getkey_artifact:{kind}:{token}"


def _amnezia_qr_payload(vpn_key: str) -> str:
    return str(vpn_key or "").removeprefix("vpn://")


def _delete_all_getkey_artifacts(context: CallbackContext, chat_id: int) -> None:
    for key in [item for item in list(context.user_data.keys()) if str(item).startswith("getkey_artifact:")]:
        msg_id = context.user_data.get(key)
        if msg_id:
            try:
                context.bot.delete_message(chat_id=chat_id, message_id=int(msg_id))
            except Exception:
                pass
        context.user_data.pop(key, None)


def _send_qr(context: CallbackContext, chat_id: int, data: str, caption: str, reply_markup=None):
    qr = _build_qr_bytes(data)
    qr.name = "key.png"
    return context.bot.send_photo(chat_id=chat_id, photo=qr, caption=caption, reply_markup=reply_markup)


def _send_main_getkey_message(context: CallbackContext, chat_id: int, text: str, reply_markup, parse_mode: str | None = PARSE_MODE) -> None:
    context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode, disable_web_page_preview=True)


def _xray_help_text(method: AccessMethod, transport: str, link: str, lang: str) -> str:
    app_hint = "Nekoray / v2rayN / Streisand / Shadowrocket"
    return (
        f"✅ *{method.label} — {transport}*\n\n"
        f"`{link}`\n\n"
        f"{t(lang, 'getkey.xray_import_title')}\n"
        f"{t(lang, 'getkey.xray_import_1', apps=app_hint)}\n"
        f"{t(lang, 'getkey.xray_import_2')}\n"
        f"{t(lang, 'getkey.xray_import_3')}"
    )


def _awg_help_text(method: AccessMethod, vpn_key: str | None, has_conf: bool, lang: str) -> str:
    lines = [f"✅ *{method.label}*"]
    if vpn_key:
        lines.extend(["", f"`{vpn_key}`", "", t(lang, "getkey.awg_import_title"), t(lang, "getkey.awg_import_1"), t(lang, "getkey.awg_import_2")])
    elif has_conf:
        lines.extend(["", t(lang, "getkey.awg_direct_missing"), "", t(lang, "getkey.awg_import_title"), t(lang, "getkey.awg_import_3"), t(lang, "getkey.awg_import_4")])
    return "\n".join(lines)


def _render_awg_main_screen(name: str, method: AccessMethod, lang: str):
    rec = get_awg_server(name, method.server_key)
    if not isinstance(rec, dict) or not (rec.get("config") or rec.get("wg_conf")):
        return t(lang, "getkey.awg_config_missing"), kb_back_to_getkey_menu([(f"server:{method.server_key}", f"{method.server.flag} {method.server.title}")], lang)
    key = _extract_vpn_key(str(rec.get("config") or ""))
    if not key:
        wg_conf = rec.get("wg_conf") or _extract_wg_conf(str(rec.get("config") or ""))
        if wg_conf and not rec.get("wg_conf"):
            rec["wg_conf"] = wg_conf
            update_awg_server(name, method.server_key, rec)
        if wg_conf:
            return _awg_help_text(method, None, True, lang), kb_awg_key_actions(method.server_key, _server_back_payload(method.server_key), lang)
        return t(lang, "getkey.awg_key_missing"), kb_awg_key_actions(method.server_key, _server_back_payload(method.server_key), lang)
    return _awg_help_text(method, key, True, lang), kb_awg_key_actions(method.server_key, _server_back_payload(method.server_key), lang)


def _render_xray_main_screen(name: str, method: AccessMethod, transport: str, lang: str):
    rec = get_profile(name)
    uuid_val = rec.get("uuid") if isinstance(rec, dict) else None
    if not uuid_val:
        return t(lang, "getkey.uuid_missing"), kb_xray_transport(method.getkey_payload, _server_back_payload(method.server_key), lang)
    try:
        link = xray_svc.build_vless_link_transport(name, uuid_val, transport, method.server_key)
    except ValueError as exc:
        return t(lang, "getkey.xray_not_ready", error=exc), kb_xray_transport(method.getkey_payload, _server_back_payload(method.server_key), lang)
    return _xray_help_text(method, transport, link, lang), kb_xray_key_actions(method.getkey_payload, transport, method.getkey_payload, lang)


def on_getkey_callback(update: Update, context: CallbackContext, payload: str) -> None:
    answer_cb(update)
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is None:
        return

    user = update.effective_user
    is_admin = _is_admin(update)
    lang = get_locale_for_update(update)

    if payload == "menu":
        _delete_all_getkey_artifacts(context, chat_id)
        _delete_all_awg_conf(context, chat_id)
        profile_name = _resolve_profile_name(user.id if user else None)
        if not user or not profile_name:
            safe_edit_message(
                update,
                context,
                t(lang, "admin.requests.profile_missing"),
                reply_markup=kb_getkey_servers([], lang),
                parse_mode=PARSE_MODE,
            )
            return

        name = profile_name
        st = get_profile_access_status(name, lang)
        if not st["active"] or st["frozen"]:
            safe_edit_message(
                update,
                context,
                t(lang, "getkey.title") + "\n\n" + st["text"],
                reply_markup=kb_getkey_servers([], lang),
                parse_mode=PARSE_MODE,
            )
            return

        text, server_items = render_getkey_overview(get_access_methods_for_codes(get_allowed_protocols(name)), lang)
        if not server_items:
            safe_edit_message(
                update,
                context,
                f"{t(lang, 'getkey.title')}\n\n{t(lang, 'getkey.no_protocols')}",
                reply_markup=kb_getkey_servers([], lang),
                parse_mode=PARSE_MODE,
            )
            return

        safe_edit_message(
            update,
            context,
            text,
            reply_markup=kb_getkey_servers(server_items, lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload.startswith("server:"):
        _delete_all_getkey_artifacts(context, chat_id)
        profile_name = _resolve_profile_name(user.id if user else None)
        if not user or not profile_name:
            return
        name = profile_name
        server_key = payload.split(":", 1)[1]
        grouped = _group_methods_by_server(get_allowed_protocols(name))
        text, items = render_server_menu(server_key, grouped.get(server_key, []), lang)
        if not items:
            safe_edit_message(
                update,
                context,
                t(lang, "getkey.server_methods_empty"),
                reply_markup=kb_getkey_servers([], lang),
                parse_mode=PARSE_MODE,
            )
            return
        safe_edit_message(
            update,
            context,
            text,
            reply_markup=kb_getkey_server_methods(server_key, items, lang),
            parse_mode=PARSE_MODE,
        )
        return

    method = get_access_method_by_getkey_payload(payload)
    if method and method.protocol_kind == "xray":
        _delete_all_getkey_artifacts(context, chat_id)
        safe_edit_message(
            update,
            context,
            f"*{method.label}*\n\n{t(lang, 'getkey.choose_transport')}",
            reply_markup=kb_xray_transport(method.getkey_payload, _server_back_payload(method.server_key), lang),
            parse_mode=PARSE_MODE,
        )
        _touch_key_stat(context, user.id)
        return

    if method and method.protocol_kind == "awg":
        _delete_all_getkey_artifacts(context, chat_id)
        profile_name = _resolve_profile_name(user.id if user else None)
        if not user or not profile_name:
            return
        name = profile_name
        server_key = method.server_key
        text, markup = _render_awg_main_screen(name, method, lang)
        safe_edit_message(update, context, text, reply_markup=markup, parse_mode=PARSE_MODE)
        _touch_key_stat(context, user.id)
        return

    if payload.startswith("xray_transport:"):
        profile_name = _resolve_profile_name(user.id if user else None)
        if not user or not profile_name:
            return
        name = profile_name
        parts = payload.split(":")
        if len(parts) != 3:
            return
        method_payload = parts[1]
        transport = parts[2]
        method = get_access_method_by_getkey_payload(method_payload)
        if not method:
            return
        rec = get_profile(name)
        uuid_val = rec.get("uuid") if isinstance(rec, dict) else None
        if not uuid_val:
            safe_edit_message(
                update,
                context,
                t(lang, "getkey.uuid_missing"),
                reply_markup=kb_xray_transport(method_payload, _server_back_payload(method.server_key), lang),
                parse_mode=PARSE_MODE,
            )
            return

        ensure_xray_caps(name, uuid_val)
        try:
            link = xray_svc.build_vless_link_transport(name, uuid_val, transport, method.server_key)
        except ValueError as exc:
            safe_edit_message(
                update,
                context,
                t(lang, "getkey.xray_not_ready", error=exc),
                reply_markup=kb_xray_transport(method_payload, _server_back_payload(method.server_key), lang),
                parse_mode=PARSE_MODE,
            )
            return
        safe_edit_message(
            update,
            context,
            _xray_help_text(method, transport, link, lang),
            reply_markup=kb_xray_key_actions(method_payload, transport, method_payload, lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload.startswith("xray_qr:"):
        _delete_all_getkey_artifacts(context, chat_id)
        profile_name = _resolve_profile_name(user.id if user else None)
        if not user or not profile_name:
            return
        name = profile_name
        parts = payload.split(":")
        if len(parts) != 3:
            return
        method_payload = parts[1]
        transport = parts[2]
        method = get_access_method_by_getkey_payload(method_payload)
        if not method:
            return
        rec = get_profile(name)
        uuid_val = rec.get("uuid") if isinstance(rec, dict) else None
        if not uuid_val:
            safe_edit_message(
                update,
                context,
                t(lang, "getkey.uuid_missing"),
                reply_markup=kb_xray_transport(method_payload, _server_back_payload(method.server_key), lang),
                parse_mode=PARSE_MODE,
            )
            return
        link = xray_svc.build_vless_link_transport(name, uuid_val, transport, method.server_key)
        sent = _send_qr(
            context,
            chat_id,
            link,
            f"{method.label} — {transport}",
            reply_markup=kb_getkey_attachment_back(f"getkey:xray_qr_back:{method_payload}:{transport}", lang),
        )
        context.user_data[_artifact_msg_key("xray_qr", f"{method_payload}:{transport}")] = sent.message_id
        safe_delete_update_message(update, context)
        return

    if payload.startswith("xray_qr_back:"):
        profile_name = _resolve_profile_name(user.id if user else None)
        if not user or not profile_name:
            return
        _delete_all_getkey_artifacts(context, chat_id)
        safe_delete_update_message(update, context)
        parts = payload.split(":")
        if len(parts) != 3:
            return
        method_payload = parts[1]
        transport = parts[2]
        method = get_access_method_by_getkey_payload(method_payload)
        if not method:
            return
        text, markup = _render_xray_main_screen(profile_name, method, transport, lang)
        _send_main_getkey_message(context, chat_id, text, markup, PARSE_MODE)
        return

    if payload.startswith("awg_qr:"):
        _delete_all_getkey_artifacts(context, chat_id)
        profile_name = _resolve_profile_name(user.id if user else None)
        if not user or not profile_name:
            return
        name = profile_name
        server_key = payload.split(":", 1)[1]
        method = get_awg_access_method_by_server_key(server_key)
        rec = get_awg_server(name, server_key)
        if not method or not isinstance(rec, dict):
            safe_edit_message(
                update,
                context,
                t(lang, "getkey.awg_profile_missing"),
                reply_markup=kb_awg_key_actions(server_key, _server_back_payload(server_key), lang),
                parse_mode=PARSE_MODE,
            )
            return
        key = _extract_vpn_key(str(rec.get("config") or ""))
        if not key:
            safe_edit_message(
                update,
                context,
                t(lang, "getkey.awg_vpn_missing"),
                reply_markup=kb_awg_key_actions(server_key, _server_back_payload(method.server_key), lang),
                parse_mode=PARSE_MODE,
            )
            return
        sent = _send_qr(
            context,
            chat_id,
            _amnezia_qr_payload(key),
            method.label,
            reply_markup=kb_getkey_attachment_back(f"getkey:awg_qr_back:{server_key}", lang),
        )
        context.user_data[_artifact_msg_key("awg_qr", server_key)] = sent.message_id
        safe_delete_update_message(update, context)
        return

    if payload.startswith("awg_qr_back:"):
        profile_name = _resolve_profile_name(user.id if user else None)
        if not user or not profile_name:
            return
        server_key = payload.split(":", 1)[1]
        method = get_awg_access_method_by_server_key(server_key)
        if not method:
            return
        _delete_all_getkey_artifacts(context, chat_id)
        safe_delete_update_message(update, context)
        text, markup = _render_awg_main_screen(profile_name, method, lang)
        _send_main_getkey_message(context, chat_id, text, markup, PARSE_MODE)
        return

    if payload.startswith("awg_conf:"):
        _delete_all_getkey_artifacts(context, chat_id)
        profile_name = _resolve_profile_name(user.id if user else None)
        if not user or not profile_name:
            return

        name = profile_name
        server_key = payload.split(":", 1)[1]
        awg_method = get_awg_access_method_by_server_key(server_key)
        rec = get_awg_server(name, server_key)
        if not isinstance(rec, dict):
            safe_edit_message(
                update,
                context,
                t(lang, "getkey.awg_profile_missing"),
                reply_markup=kb_awg_key_actions(server_key, _server_back_payload(awg_method.server_key) if awg_method else None, lang),
                parse_mode=PARSE_MODE,
            )
            return

        wg_conf = rec.get("wg_conf")
        if not wg_conf:
            wg_conf = _extract_wg_conf(rec.get("config", "") or "")
            if wg_conf:
                rec["wg_conf"] = wg_conf
                update_awg_server(name, server_key, rec)

        if not wg_conf:
            safe_edit_message(
                update,
                context,
                t(lang, "getkey.awg_conf_extract_failed"),
                reply_markup=kb_awg_key_actions(server_key, _server_back_payload(awg_method.server_key) if awg_method else None, lang),
                parse_mode=PARSE_MODE,
            )
            return

        conf_io = io.BytesIO(wg_conf.encode("utf-8"))
        conf_io.name = f"{name}_{server_key}.conf"
        sent = context.bot.send_document(
            chat_id=chat_id,
            document=conf_io,
            caption=t(lang, "getkey.awg_conf_caption"),
            reply_markup=kb_getkey_attachment_back(f"getkey:awg_conf_back:{server_key}", lang),
        )
        context.user_data[_artifact_msg_key("awg_conf", server_key)] = sent.message_id
        context.user_data[_conf_msg_key(server_key)] = sent.message_id
        safe_delete_update_message(update, context)
        return

    if payload.startswith("awg_conf_back:"):
        profile_name = _resolve_profile_name(user.id if user else None)
        if not user or not profile_name:
            return
        server_key = payload.split(":", 1)[1]
        method = get_awg_access_method_by_server_key(server_key)
        if not method:
            return
        _delete_all_getkey_artifacts(context, chat_id)
        safe_delete_update_message(update, context)
        text, markup = _render_awg_main_screen(profile_name, method, lang)
        _send_main_getkey_message(context, chat_id, text, markup, PARSE_MODE)
        return

    _delete_all_getkey_artifacts(context, chat_id)
    safe_edit_message(
        update,
        context,
        f"*{get_menu_title_markdown()}*\n\n{t(lang, 'menu.choose_action')}",
        reply_markup=kb_main_menu(is_admin, True, lang),
        parse_mode=PARSE_MODE,
    )
