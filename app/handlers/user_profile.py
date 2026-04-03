# app/handlers/user_profile.py
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext

from config import ADMIN_IDS, APP_VERSION, CB_SRV, INSTALL_MODE, LIST_PAGE_SIZE, PARSE_MODE
from domain.servers import get_access_methods_for_codes
from i18n import get_locale_for_update, get_user_locale, set_user_locale, t
from services.app_settings import (
    are_access_requests_enabled,
    get_backups_interval_hours,
    get_backups_keep_count,
    get_access_gate_message,
    get_menu_title,
    get_menu_title_markdown,
    get_updates_branch,
    is_backups_enabled,
    is_updates_auto_check_enabled,
    is_global_telemetry_enabled,
    set_backups_enabled,
    set_backups_interval_hours,
    set_backups_keep_count,
    set_access_gate_message,
    set_access_requests_enabled,
    set_updates_auto_check_enabled,
    set_updates_branch,
    set_global_telemetry_enabled,
    set_initial_setup_state,
    set_menu_title,
    should_show_initial_admin_setup,
)
from services.backups import backup_token, create_backup, get_backup_info, get_backups_overview, list_backups, resolve_backup_token, restore_backup
from services.provisioning_state import summarize_server_provisioning
from services.server_bootstrap import get_server_runtime_state, get_servers_needing_runtime_sync, sync_server_runtime
from services.server_registry import list_servers
from services.awg_profiles import list_awg_server_keys
from services.ssh_keys import render_public_key_guide, render_public_key_summary
from services.system_reset import run_factory_reset
from services.profile_state import ensure_telegram_profile, get_allowed_protocols, get_profile, get_profile_access_status, profile_store, user_store, utcnow
from services.traffic_usage import get_profile_monthly_usage
from services.updates import check_for_updates, get_updates_menu_emoji, get_updates_overview, get_version_transition, list_available_versions, schedule_update
from services.xray import get_server_link_status
from ui.user_views import format_server_access
from utils.keyboards import kb_admin_backups_menu, kb_admin_backups_settings_menu, kb_admin_menu, kb_admin_requests_settings_menu, kb_admin_settings_menu, kb_admin_updates_branch_menu, kb_admin_updates_menu, kb_back_to_admin, kb_language_menu, kb_main_menu, kb_profile_minimal, kb_profile_stats, kb_settings_menu
from utils.tg import answer_cb, safe_delete_update_message, safe_edit_by_ids, safe_edit_message

from .user_common import _access_gate_text, _build_start_reply, _has_access, _human_ago, _human_left, _is_admin, _resolve_profile_name, _sub_progress


def _md(value: Any) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("*", "\\*")
        .replace("_", "\\_")
        .replace("[", "\\[")
    )


def _render_admin_menu_text(lang: str) -> str:
    return f"{t(lang, 'admin.menu_title')}\n\n{t(lang, 'menu.admin_choose')}"


def _admin_updates_menu_label(lang: str) -> str:
    overview = get_updates_overview()
    base = t(lang, "menu.updates_plain")
    return f"{get_updates_menu_emoji(overview)} {base}"


def _admin_menu_markup(lang: str) -> InlineKeyboardMarkup:
    return kb_admin_menu(lang, updates_label=_admin_updates_menu_label(lang))


def _render_admin_setup_text(lang: str) -> str:
    return (
        f"{t(lang, 'admin.setup.title')}\n\n"
        f"{t(lang, 'admin.setup.intro')}\n\n"
        f"{t(lang, 'admin.setup.local_hint')}\n"
        f"{t(lang, 'admin.setup.remote_hint')}\n\n"
        f"{t(lang, 'admin.setup.note')}"
    )


def _render_admin_setup_markup(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t(lang, "admin.setup.local_button"), callback_data=f"{CB_SRV}start:create_local")],
            [InlineKeyboardButton(t(lang, "admin.setup.remote_button"), callback_data=f"{CB_SRV}start:create_remote")],
            [InlineKeyboardButton(t(lang, "admin.setup.later"), callback_data="menu:admin_setup_later")],
        ]
    )


def _render_admin_status(lang: str) -> str:
    servers = list_servers(include_disabled=True)
    runtime_states = {server.key: get_server_runtime_state(server.key) for server in servers if server.bootstrap_state == "bootstrapped"}
    subs = profile_store.read()
    users = user_store.read()
    profile_names = [str(name) for name in subs.keys() if not str(name).startswith("_")] if isinstance(subs, dict) else []
    profiles_total = len(profile_names)
    pending_requests = sum(1 for rec in users.values() if isinstance(rec, dict) and rec.get("access_request_pending"))
    active_servers = sum(1 for server in servers if server.enabled)
    active_profiles = 0
    frozen_profiles = 0
    for name in profile_names:
        st = get_profile_access_status(name)
        if st.get("frozen"):
            frozen_profiles += 1
        if st.get("active"):
            active_profiles += 1
    lines = [
        t(lang, "admin.status.title"),
        "",
        t(lang, "admin.status.overview"),
        t(lang, "admin.status.version", version=APP_VERSION),
        t(lang, "admin.status.servers", active=active_servers, total=len(servers)),
        t(lang, "admin.status.profiles_total", active=active_profiles, total=profiles_total),
        t(lang, "admin.status.requests_pending", count=pending_requests),
        "",
        t(lang, "admin.status.health_summary"),
    ]
    if not servers:
        lines.append(t(lang, "admin.status.no_servers"))
    else:
        ready_servers = sum(1 for server in servers if server.bootstrap_state == "bootstrapped")
        needs_attention = 0
        xray_ready = 0
        awg_ready = 0
        runtime_drift = 0
        for server in servers:
            prov = summarize_server_provisioning(server.key)
            xray_ok = True
            runtime_state = str((runtime_states.get(server.key) or {}).get("state") or "")
            if "xray" in server.protocol_kinds:
                xray_ok = get_server_link_status(server.key)[0]
            awg_ok = ("awg" not in server.protocol_kinds) or server.bootstrap_state == "bootstrapped"
            if runtime_state in {"outdated", "unknown"}:
                runtime_drift += 1
            if (
                server.bootstrap_state != "bootstrapped"
                or prov["overall"] in {"failed", "needs_attention"}
                or not xray_ok
                or not awg_ok
                or runtime_state in {"outdated", "unknown"}
            ):
                needs_attention += 1
            if "xray" in server.protocol_kinds and xray_ok:
                xray_ready += 1
            if "awg" in server.protocol_kinds and server.bootstrap_state == "bootstrapped":
                awg_ready += 1
        lines.append(t(lang, "admin.status.bootstrap_ready", icon="•" if ready_servers == len(servers) else "!", ready=ready_servers, total=len(servers)))
        lines.append(t(lang, "admin.status.xray_ready", icon="•" if xray_ready else "!", count=xray_ready))
        lines.append(t(lang, "admin.status.awg_ready", icon="•" if awg_ready else "!", count=awg_ready))
        lines.append(t(lang, "admin.status.runtime_drift", icon="•" if runtime_drift == 0 else "!", count=runtime_drift))
        lines.append(t(lang, "admin.status.needs_attention", icon="•" if needs_attention == 0 else "!", count=needs_attention))

    lines.extend(
        [
            "",
            t(lang, "admin.status.queue"),
            t(lang, "admin.status.requests_pending_line", count=pending_requests),
            t(lang, "admin.status.frozen_profiles", count=frozen_profiles),
        ]
    )

    action_items: List[str] = []
    for server in servers:
        if len(action_items) >= 3:
            break
        if not server.enabled:
            continue
        if server.bootstrap_state != "bootstrapped":
            action_items.append(t(lang, "admin.status.action_bootstrap", server=server.key))
            continue
        runtime_state = str((runtime_states.get(server.key) or {}).get("state") or "")
        if runtime_state in {"outdated", "unknown"}:
            action_items.append(t(lang, "admin.status.action_runtime_sync", server=server.key))
            continue
        xray_ready, reason = get_server_link_status(server.key) if "xray" in server.protocol_kinds else (True, "ok")
        if "xray" in server.protocol_kinds and not xray_ready:
            if "incomplete" in reason:
                action_items.append(t(lang, "admin.status.action_xray_link", server=server.key))
            else:
                action_items.append(t(lang, "admin.status.action_xray_runtime", server=server.key))
            continue
        prov = summarize_server_provisioning(server.key)
        if prov["overall"] in {"failed", "needs_attention"}:
            action_items.append(t(lang, "admin.status.action_provisioning", server=server.key))

    if action_items or pending_requests:
        lines.extend(["", t(lang, "admin.status.needs_action")])
        lines.extend(action_items)
        if pending_requests and len(action_items) < 3:
            lines.append(t(lang, "admin.status.action_requests", count=pending_requests))
    return "\n".join(lines)


def _problem_server_keys() -> List[str]:
    keys: List[str] = []
    for server in list_servers(include_disabled=False):
        if server.bootstrap_state != "bootstrapped":
            keys.append(server.key)
            continue
        if str(get_server_runtime_state(server.key).get("state") or "") in {"outdated", "unknown"}:
            keys.append(server.key)
            continue
        if "xray" in server.protocol_kinds and not get_server_link_status(server.key)[0]:
            keys.append(server.key)
            continue
        prov = summarize_server_provisioning(server.key)
        if prov["overall"] in {"failed", "needs_attention"}:
            keys.append(server.key)
    return keys


def _runtime_drift_server_keys() -> List[str]:
    return [server.key for server in get_servers_needing_runtime_sync()]


def _render_problem_servers(lang: str) -> tuple[str, InlineKeyboardMarkup]:
    rows: List[List[InlineKeyboardButton]] = []
    keys = _problem_server_keys()
    if not keys:
        return (
            t(lang, "admin.status.problem_servers_empty"),
            InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "menu.back"), callback_data="menu:admin_status")]]),
        )
    servers = {server.key: server for server in list_servers(include_disabled=False)}
    lines = [t(lang, "admin.status.problem_servers_title"), ""]
    for key in keys:
        server = servers.get(key)
        if not server:
            continue
        reason = t(lang, "admin.status.problem_server_reason_bootstrap")
        if server.bootstrap_state == "bootstrapped":
            runtime_state = str(get_server_runtime_state(server.key).get("state") or "")
            xray_ready, reason_text = get_server_link_status(server.key) if "xray" in server.protocol_kinds else (True, "ok")
            if runtime_state in {"outdated", "unknown"}:
                reason = t(lang, "admin.status.problem_server_reason_runtime_sync")
            elif "xray" in server.protocol_kinds and not xray_ready:
                reason = (
                    t(lang, "admin.status.problem_server_reason_xray_link")
                    if "incomplete" in reason_text
                    else t(lang, "admin.status.problem_server_reason_xray_runtime")
                )
            else:
                prov = summarize_server_provisioning(server.key)
                if prov["overall"] in {"failed", "needs_attention"}:
                    reason = t(lang, "admin.status.problem_server_reason_provisioning")
        lines.append(
            t(
                lang,
                "admin.status.problem_server_line",
                flag=server.flag,
                title=server.title,
                server_key=server.key,
                reason=reason,
            )
        )
        rows.append([InlineKeyboardButton(f"{server.flag} {server.title}", callback_data=f"{CB_SRV}card:{server.key}")])
    rows.append([InlineKeyboardButton(t(lang, "menu.back"), callback_data="menu:admin_status")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _render_runtime_sync_confirm(lang: str) -> tuple[str, InlineKeyboardMarkup]:
    targets = get_servers_needing_runtime_sync()
    if not targets:
        return (
            t(lang, "admin.status.runtime_sync_empty"),
            InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "menu.back"), callback_data="menu:admin_status")]]),
        )
    lines = [
        t(lang, "admin.status.runtime_sync_confirm_title"),
        "",
        t(lang, "admin.status.runtime_sync_confirm_intro", count=len(targets)),
    ]
    lines.extend([f"• {server.flag} {server.title} ({server.key})" for server in targets[:10]])
    if len(targets) > 10:
        lines.append(t(lang, "admin.status.runtime_sync_confirm_more", count=len(targets) - 10))
    rows = [
        [InlineKeyboardButton(t(lang, "admin.status.runtime_sync_confirm_action"), callback_data="menu:admin_runtime_sync_run")],
        [InlineKeyboardButton(t(lang, "menu.back"), callback_data="menu:admin_status")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _sync_runtime_drift(lang: str) -> str:
    targets = get_servers_needing_runtime_sync()
    if not targets:
        return t(lang, "admin.status.runtime_sync_empty")

    updated: List[str] = []
    failed: List[str] = []
    for server in targets:
        rc, out = sync_server_runtime(server.key)
        if rc == 0:
            updated.append(server.key)
        else:
            tail = str(out or "").strip().splitlines()
            failed.append(f"{server.key}: {tail[-1] if tail else 'unknown error'}")

    lines = [t(lang, "admin.status.runtime_sync_result_title"), ""]
    lines.append(t(lang, "admin.status.runtime_sync_result_updated", count=len(updated)))
    lines.append(t(lang, "admin.status.runtime_sync_result_failed", count=len(failed)))
    if updated:
        lines.extend(["", t(lang, "admin.status.runtime_sync_result_updated_list")])
        lines.extend([f"• {key}" for key in updated])
    if failed:
        lines.extend(["", t(lang, "admin.status.runtime_sync_result_failed_list")])
        lines.extend([f"• {item}" for item in failed[:10]])
    return "\n".join(lines)


def _ssh_key_summary_markup(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t(lang, "ssh.details"), callback_data="menu:sshkey_details")],
            [InlineKeyboardButton(t(lang, "menu.back"), callback_data="menu:admin")],
        ]
    )


def _ssh_key_details_markup(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t(lang, "menu.back"), callback_data="menu:sshkey")],
        ]
    )


def _kb_admin_status(lang: str) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    if _all_pending_request_ids():
        rows.append([InlineKeyboardButton(t(lang, "menu.requests"), callback_data="menu:admin_requests")])
    if _problem_server_keys():
        rows.append([InlineKeyboardButton(t(lang, "admin.status.problem_servers_button"), callback_data="menu:admin_problem_servers")])
    if _runtime_drift_server_keys():
        rows.append([InlineKeyboardButton(t(lang, "admin.status.runtime_sync_button"), callback_data="menu:admin_runtime_sync_all")])
    rows.append([InlineKeyboardButton(t(lang, "menu.back"), callback_data="menu:admin")])
    return InlineKeyboardMarkup(rows)


def _format_username(value: str, lang: str) -> str:
    username = value.strip()
    if not username:
        return t(lang, "common.none")
    return f"@{username}"


def _format_bytes(num_bytes: int) -> str:
    value = float(max(0, int(num_bytes)))
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(value)} {units[idx]}"
    return f"{value:.1f} {units[idx]}"


def _request_state_get(context: CallbackContext) -> Optional[Dict[str, Any]]:
    state = context.user_data.get("access_requests")
    return state if isinstance(state, dict) else None


def _request_state_set(context: CallbackContext, state: Dict[str, Any]) -> None:
    context.user_data["access_requests"] = state


def _request_state_clear(context: CallbackContext) -> None:
    context.user_data.pop("access_requests", None)


def _request_capture_message(update: Update, context: CallbackContext) -> None:
    state = _request_state_get(context) or {}
    q = update.callback_query
    if q and q.message:
        state["chat_id"] = q.message.chat_id
        state["message_id"] = q.message.message_id
        _request_state_set(context, state)


def _request_edit(context: CallbackContext, text: str, reply_markup: Any, parse_mode: Optional[str] = PARSE_MODE) -> bool:
    state = _request_state_get(context)
    if not state:
        return False
    chat_id = state.get("chat_id")
    message_id = state.get("message_id")
    if not chat_id or not message_id:
        return False
    safe_edit_by_ids(context.bot, int(chat_id), int(message_id), text, reply_markup=reply_markup, parse_mode=parse_mode)
    return True


def _announce_state_get(context: CallbackContext) -> Optional[Dict[str, Any]]:
    state = context.user_data.get("admin_announce")
    return state if isinstance(state, dict) else None


def _announce_state_set(context: CallbackContext, state: Dict[str, Any]) -> None:
    context.user_data["admin_announce"] = state


def _announce_state_clear(context: CallbackContext) -> None:
    context.user_data.pop("admin_announce", None)


def _announce_capture_message(update: Update, context: CallbackContext) -> None:
    state = _announce_state_get(context) or {}
    q = update.callback_query
    if q and q.message:
        state["chat_id"] = q.message.chat_id
        state["message_id"] = q.message.message_id
        _announce_state_set(context, state)


def _announce_edit(context: CallbackContext, text: str, reply_markup: Any, parse_mode: Optional[str] = PARSE_MODE) -> bool:
    state = _announce_state_get(context)
    if not state:
        return False
    chat_id = state.get("chat_id")
    message_id = state.get("message_id")
    if not chat_id or not message_id:
        return False
    safe_edit_by_ids(context.bot, int(chat_id), int(message_id), text, reply_markup=reply_markup, parse_mode=parse_mode)
    return True


def _announce_confirm_markup(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(t(lang, "admin.announce.edit"), callback_data="menu:admin_announce_edit"),
                InlineKeyboardButton(t(lang, "admin.announce.send"), callback_data="menu:admin_announce_send"),
            ],
            [InlineKeyboardButton(t(lang, "admin.announce.cancel"), callback_data="menu:admin_announce_cancel")],
        ]
    )


def _announce_confirm_text(lang: str, draft_text: str) -> str:
    return f"{t(lang, 'admin.announce.confirm')}\n\n{draft_text}"


def _all_pending_request_ids() -> List[str]:
    users = user_store.read()
    result: List[str] = []
    for user_id, rec in users.items():
        if not isinstance(rec, dict):
            continue
        if rec.get("access_request_pending"):
            result.append(str(user_id))
    return sorted(result, key=lambda value: int(value) if str(value).isdigit() else str(value))


def _request_label(user_id: str, rec: Dict[str, Any]) -> str:
    username = str(rec.get("username") or "").strip()
    if username:
        return f"@{username}"
    full_name = " ".join(part for part in [str(rec.get("first_name") or "").strip(), str(rec.get("last_name") or "").strip()] if part)
    return full_name or f"id:{user_id}"


def _render_requests_dashboard(ids: List[str], page: int, lang: str) -> tuple[str, Any]:
    total = len(ids)
    if total == 0:
        return (
            t(lang, "admin.requests.empty"),
            InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton(t(lang, "menu.back"), callback_data="menu:admin")],
                ]
            ),
        )

    pages = max(1, (total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    chunk = ids[page * LIST_PAGE_SIZE : (page + 1) * LIST_PAGE_SIZE]
    users = user_store.read()

    rows = []
    for user_id in chunk:
        rec = users.get(str(user_id)) if isinstance(users, dict) else None
        if not isinstance(rec, dict):
            continue
        rows.append([InlineKeyboardButton(f"👤 {_request_label(str(user_id), rec)}", callback_data=f"menu:admin_request_card:{user_id}")])

    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"menu:admin_requests_page:{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data=f"menu:admin_requests_page:{page}"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"menu:admin_requests_page:{page+1}"))
        rows.append(nav)
    if pages > 1:
        rows.append([InlineKeyboardButton(t(lang, "admin.requests.search"), callback_data="menu:admin_requests_search")])
    rows.append([InlineKeyboardButton(t(lang, "menu.back"), callback_data="menu:admin")])
    return t(lang, "admin.requests.title", total=total), InlineKeyboardMarkup(rows)


def _render_request_card(user_id: str, lang: str) -> tuple[str, Any]:
    users = user_store.read()
    rec = users.get(str(user_id)) if isinstance(users, dict) else None
    if not isinstance(rec, dict):
        return t(lang, "admin.requests.user_missing"), kb_back_to_admin(lang)

    username = str(rec.get("username") or "").strip()
    username_text = f"@{username}" if username else "—"
    full_name = " ".join(part for part in [str(rec.get("first_name") or "").strip(), str(rec.get("last_name") or "").strip()] if part) or "—"
    requested_at = str(rec.get("access_request_sent_at") or "—")
    if rec.get("access_request_pending"):
        status_text = t(lang, "admin.requests.status_pending")
    elif rec.get("access_granted"):
        status_text = t(lang, "admin.requests.status_approved")
    else:
        status_text = t(lang, "admin.requests.status_rejected")

    text = (
        f"{t(lang, 'admin.requests.card_title')}\n\n"
        f"{t(lang, 'admin.requests.identity')}\n"
        f"• id: `{_md(user_id)}`\n"
        f"• username: {_md(username_text)}\n"
        f"• name: {_md(full_name)}\n\n"
        f"{t(lang, 'admin.requests.request_meta')}\n"
        f"• {t(lang, 'admin.requests.requested_at')}: `{_md(requested_at)}`\n"
        f"• status: *{status_text}*"
    )
    rows = []
    if rec.get("access_request_pending"):
        rows.append(
            [
                InlineKeyboardButton(t(lang, "admin.requests.approve"), callback_data=f"menu:admin_request_approve:{user_id}"),
                InlineKeyboardButton(t(lang, "admin.requests.reject"), callback_data=f"menu:admin_request_reject:{user_id}"),
            ]
        )
    rows.append([InlineKeyboardButton(t(lang, "admin.requests.to_list"), callback_data="menu:admin_requests")])
    return (text, InlineKeyboardMarkup(rows))


def _open_requests_dashboard(update: Update, context: CallbackContext, lang: str, *, page: int = 0, ids: Optional[List[str]] = None) -> None:
    _request_capture_message(update, context)
    state = _request_state_get(context) or {}
    if ids is None:
        ids = state.get("ids") if isinstance(state.get("ids"), list) else _all_pending_request_ids()
    state.update({"active": True, "step": "dashboard", "page": page, "ids": ids})
    _request_state_set(context, state)
    text, markup = _render_requests_dashboard(ids, page, lang)
    safe_edit_message(update, context, text, reply_markup=markup, parse_mode=PARSE_MODE)


def _set_admin_flag(user_id: int, **fields: Any) -> None:
    user_store.upsert_user(user_id, **fields)


def _ensure_profile_for_request(user_id: int) -> str:
    return ensure_telegram_profile(user_id)


def _admin_notify_enabled(user_id: int) -> bool:
    users = user_store.read()
    rec = users.get(str(user_id)) if isinstance(users, dict) else None
    if not isinstance(rec, dict):
        return True
    return bool(rec.get("notify_access_requests", True))


def _user_announcement_silent(user_id: int) -> bool:
    users = user_store.read()
    rec = users.get(str(user_id)) if isinstance(users, dict) else None
    if not isinstance(rec, dict):
        return False
    return bool(rec.get("announcement_silent", False))


def _user_telemetry_enabled(user_id: int) -> bool:
    users = user_store.read()
    rec = users.get(str(user_id)) if isinstance(users, dict) else None
    if not isinstance(rec, dict):
        return False
    return bool(rec.get("telemetry_enabled", False))


def _admin_settings_state_get(context: CallbackContext) -> Optional[Dict[str, Any]]:
    state = context.user_data.get("admin_settings")
    return state if isinstance(state, dict) else None


def _admin_settings_state_set(context: CallbackContext, state: Dict[str, Any]) -> None:
    context.user_data["admin_settings"] = state


def _admin_settings_state_clear(context: CallbackContext) -> None:
    context.user_data.pop("admin_settings", None)


def _admin_settings_capture_message(update: Update, context: CallbackContext) -> None:
    state = _admin_settings_state_get(context) or {}
    q = update.callback_query
    if q and q.message:
        state["chat_id"] = q.message.chat_id
        state["message_id"] = q.message.message_id
        _admin_settings_state_set(context, state)


def _render_admin_settings_text(lang: str) -> str:
    return t(lang, "admin.settings.title")


def _render_admin_requests_settings_text(lang: str) -> str:
    return t(lang, "admin.settings.requests_title")


def _render_admin_reset_text(lang: str) -> str:
    return "\n".join(
        [
            t(lang, "admin.settings.reset_title"),
            "",
            t(lang, "admin.settings.reset_intro"),
            "",
            t(lang, "admin.settings.reset_scope"),
        ]
    )


def _render_admin_reset_confirm_text(scope: str, lang: str) -> str:
    lines = [
        t(lang, "admin.settings.reset_confirm_title"),
        "",
        t(lang, "admin.settings.reset_confirm_local"),
    ]
    if scope in {"nodes", "nodes_ssh"}:
        lines.extend(["", t(lang, "admin.settings.reset_confirm_nodes")])
    if scope == "nodes_ssh":
        lines.extend(["", t(lang, "admin.settings.reset_confirm_portable")])
    return "\n".join(lines)


def _admin_reset_markup(lang: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(t(lang, "admin.settings.reset_local_only"), callback_data="menu:admin_settings_reset_scope:local")],
        [InlineKeyboardButton(t(lang, "admin.settings.reset_with_nodes"), callback_data="menu:admin_settings_reset_scope:nodes")],
    ]
    if str(INSTALL_MODE or "").strip().lower() == "portable":
        rows.append([InlineKeyboardButton(t(lang, "admin.settings.reset_with_nodes_ssh"), callback_data="menu:admin_settings_reset_scope:nodes_ssh")])
    rows.append([InlineKeyboardButton(t(lang, "menu.back"), callback_data="menu:admin_settings")])
    return InlineKeyboardMarkup(rows)


def _admin_reset_confirm_markup(scope: str, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t(lang, "admin.settings.reset_confirm_action"), callback_data=f"menu:admin_settings_reset_run:{scope}")],
            [InlineKeyboardButton(t(lang, "admin.settings.reset_cancel"), callback_data="menu:admin_settings_reset")],
        ]
    )


def _updates_status_label(status: str, lang: str) -> str:
    normalized = str(status or "").strip().lower()
    mapping = {
        "never": "admin.updates.status_never",
        "up_to_date": "admin.updates.status_up_to_date",
        "available": "admin.updates.status_available",
        "error": "admin.updates.status_error",
    }
    return t(lang, mapping.get(normalized, "admin.updates.status_error"))


def _updates_run_status_label(status: str, lang: str) -> str:
    normalized = str(status or "").strip().lower()
    mapping = {
        "never": "admin.updates.run_never",
        "running": "admin.updates.run_running",
        "success": "admin.updates.run_success",
        "failed": "admin.updates.run_failed",
    }
    return t(lang, mapping.get(normalized, "admin.updates.run_failed"))


def _admin_updates_markup(lang: str) -> InlineKeyboardMarkup:
    overview = get_updates_overview()
    update_running = str(overview.get("last_run_status") or "") == "running"
    show_update_action = bool(overview.get("update_supported")) and (bool(overview.get("update_available")) or update_running)
    return kb_admin_updates_menu(
        bool(overview.get("auto_check_enabled")),
        show_update_action,
        update_running,
        str(overview.get("branch") or get_updates_branch()),
        lang,
    )


def _render_admin_updates_text(lang: str, include_failure_log: bool = True) -> str:
    overview = get_updates_overview()
    last_checked = str(overview.get("last_checked_at") or "").strip()
    last_checked_value = _human_ago(last_checked, lang) if last_checked else t(lang, "common.none")
    auto_check_value = t(lang, "admin.updates.auto_check_enabled") if overview.get("auto_check_enabled") else t(lang, "admin.updates.auto_check_disabled")
    last_run_status = str(overview.get("last_run_status") or "never")
    last_run_at = str(overview.get("last_run_finished_at") or overview.get("last_run_started_at") or "").strip()
    last_run_value = _updates_run_status_label(last_run_status, lang)
    if last_run_at:
        last_run_value = f"{last_run_value} · {_human_ago(last_run_at, lang)}"
    lines = [
        t(lang, "admin.updates.title"),
        "",
        t(lang, "admin.updates.section_status"),
        t(lang, "admin.updates.branch", value=str(overview.get("branch") or get_updates_branch())),
        t(lang, "admin.updates.auto_check", value=auto_check_value),
        t(lang, "admin.updates.last_checked", value=last_checked_value),
        t(lang, "admin.updates.last_status", value=_updates_status_label(str(overview.get("last_status") or "never"), lang)),
        "",
        t(lang, "admin.updates.section_source"),
        t(lang, "admin.updates.install_mode", value=t(lang, f"admin.updates.mode_{overview.get('install_mode', 'portable')}")),
        t(lang, "admin.updates.current_version", value=str(overview.get("current_version") or "—")),
        t(lang, "admin.updates.source_dir", value=str(overview.get("source_dir") or "—")),
        "",
        t(lang, "admin.updates.section_last_run"),
        t(lang, "admin.updates.last_update", value=last_run_value),
    ]
    latest_version = str(overview.get("remote_label") or "").strip()
    if latest_version:
        lines.append(t(lang, "admin.updates.latest_version", value=latest_version))
    last_error = str(overview.get("last_error") or "").strip()
    if last_error:
        lines.extend(["", t(lang, "admin.updates.section_error"), t(lang, "admin.updates.error", value=last_error)])
    last_run_log_tail = str(overview.get("last_run_log_tail") or "").strip()
    if include_failure_log and last_run_log_tail and last_run_status == "failed":
        lines.extend(
            [
                "",
                t(lang, "admin.updates.last_update_log"),
                _md(last_run_log_tail),
            ]
        )
    return "\n".join(lines)


def _render_admin_updates_branch_text(lang: str) -> str:
    branch = get_updates_branch()
    return "\n".join(
        [
            t(lang, "admin.updates.branch_title"),
            "",
            t(lang, "admin.updates.branch_current", value=branch),
            "",
            t(lang, "admin.updates.branch_intro"),
            t(lang, "admin.updates.branch_hint_main"),
            t(lang, "admin.updates.branch_hint_dev"),
        ]
    )


def _render_admin_updates_versions_page(lang: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    branch = get_updates_branch()
    result = list_available_versions(branch)
    versions = list(result.get("versions") or [])
    if not versions:
        text = f"{t(lang, 'admin.updates.versions_title')}\n\n{t(lang, 'admin.updates.versions_empty', branch=branch)}"
        return text, InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "menu.back"), callback_data="menu:admin_updates")]])

    total = len(versions)
    pages = max(1, (total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    chunk = versions[page * LIST_PAGE_SIZE : (page + 1) * LIST_PAGE_SIZE]
    rows: List[List[InlineKeyboardButton]] = []
    for item in chunk:
        action = str(item.get("action") or "blocked")
        if action == "current":
            icon = "•"
        elif action == "upgrade":
            icon = "↑"
        elif action == "downgrade":
            icon = "↓"
        else:
            icon = "×"
        if action == "current":
            callback = f"menu:admin_updates_versions:{page}"
        elif action == "upgrade":
            callback = f"menu:admin_updates_version:{item['ref']}"
        elif action == "downgrade":
            callback = f"menu:admin_updates_version:{item['ref']}"
        else:
            callback = f"menu:admin_updates_versions:{page}"
        rows.append([InlineKeyboardButton(f"{icon} {item['version']}", callback_data=callback)])
    if pages > 1:
        nav: List[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"menu:admin_updates_versions:{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data=f"menu:admin_updates_versions:{page}"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"menu:admin_updates_versions:{page+1}"))
        rows.append(nav)
    rows.append([InlineKeyboardButton(t(lang, "menu.back"), callback_data="menu:admin_updates")])
    current_version = str(result.get("current_version") or get_updates_overview().get("current_version") or "—")
    text = "\n".join(
        [
            t(lang, "admin.updates.versions_title"),
            "",
            t(lang, "admin.updates.versions_intro", branch=branch),
            t(lang, "admin.updates.version_confirm_current", value=current_version),
            t(lang, "admin.updates.versions_legend"),
        ]
    )
    return text, InlineKeyboardMarkup(rows)


def _render_admin_updates_version_confirm(lang: str, ref: str) -> tuple[str, InlineKeyboardMarkup]:
    versions = list_available_versions(get_updates_branch()).get("versions") or []
    item = next((entry for entry in versions if str(entry.get("ref")) == ref), None)
    if not item:
        return _render_admin_updates_versions_page(lang, 0)
    target_version = str(item.get("version") or "")
    transition = get_version_transition(str(get_updates_overview().get("current_version") or ""), target_version)
    lines = [
        t(lang, "admin.updates.version_confirm_title"),
        "",
        t(lang, "admin.updates.version_confirm_current", value=str(get_updates_overview().get("current_version") or "—")),
        t(lang, "admin.updates.version_confirm_target", value=target_version),
    ]
    if transition.get("action") in {"upgrade", "downgrade"}:
        lines.append(t(lang, "admin.updates.version_confirm_action", value=t(lang, f"admin.updates.action_{transition['action']}")))
    reason = str(transition.get("reason") or "")
    if reason == "major_upgrade":
        lines.extend(["", t(lang, "admin.updates.version_confirm_warning_major")])
    elif reason == "major_downgrade_blocked":
        lines.extend(["", t(lang, "admin.updates.version_confirm_blocked_major")])
    elif reason == "pre1_minor_downgrade_blocked":
        lines.extend(["", t(lang, "admin.updates.version_confirm_blocked_pre1")])
    rows = []
    if bool(transition.get("allowed")):
        rows.append([InlineKeyboardButton(t(lang, "admin.updates.version_install"), callback_data=f"menu:admin_updates_install:{ref}")])
    rows.append([InlineKeyboardButton(t(lang, "menu.back"), callback_data="menu:admin_updates_versions:0")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _human_size(size_bytes: int) -> str:
    value = float(max(0, int(size_bytes)))
    units = ["B", "KiB", "MiB", "GiB"]
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(value)} {units[idx]}"
    return f"{value:.1f} {units[idx]}"


def _backups_run_status_label(status: str, lang: str) -> str:
    normalized = str(status or "").strip().lower()
    mapping = {
        "never": "admin.backups.run_never",
        "success": "admin.backups.run_success",
        "failed": "admin.backups.run_failed",
        "skipped_duplicate": "admin.backups.run_skipped_duplicate",
    }
    return t(lang, mapping.get(normalized, "admin.backups.run_failed"))


def _backups_restore_status_label(status: str, lang: str) -> str:
    normalized = str(status or "").strip().lower()
    mapping = {
        "never": "admin.backups.restore_never",
        "success": "admin.backups.restore_success",
        "failed": "admin.backups.restore_failed",
    }
    return t(lang, mapping.get(normalized, "admin.backups.restore_failed"))


def _backup_trigger_label(trigger: str, lang: str) -> str:
    normalized = str(trigger or "").strip().lower()
    key = f"admin.backups.trigger_{normalized.replace('-', '_')}"
    try:
        return t(lang, key)
    except Exception:
        return t(lang, "admin.backups.trigger_unknown")


def _render_admin_backups_text(lang: str) -> str:
    overview = get_backups_overview()
    last_run_at = str(overview.get("last_run_at") or "").strip()
    last_restore_at = str(overview.get("last_restore_at") or "").strip()
    last_run_value = _backups_run_status_label(str(overview.get("last_status") or "never"), lang)
    if last_run_at:
        last_run_value = f"{last_run_value} · {_human_ago(last_run_at, lang)}"
    last_restore_value = _backups_restore_status_label(str(overview.get("last_restore_status") or "never"), lang)
    if last_restore_at:
        last_restore_value = f"{last_restore_value} · {_human_ago(last_restore_at, lang)}"
    lines = [
        t(lang, "admin.backups.title"),
        "",
        t(lang, "admin.backups.section_status"),
        t(lang, "admin.backups.status", value=t(lang, "admin.backups.enabled") if overview.get("enabled") else t(lang, "admin.backups.disabled")),
        t(lang, "admin.backups.interval", value=f"{overview.get('interval_hours', 24)}h"),
        t(lang, "admin.backups.keep_count", value=str(overview.get("keep_count", 10))),
        t(lang, "admin.backups.last_run", value=last_run_value),
        t(lang, "admin.backups.last_status", value=_backups_run_status_label(str(overview.get("last_status") or "never"), lang)),
        "",
        t(lang, "admin.backups.section_storage"),
        t(lang, "admin.backups.total_files", value=str(overview.get("total_backups", 0))),
        t(lang, "admin.backups.total_size", value=_human_size(int(overview.get("total_size_bytes") or 0))),
        "",
        t(lang, "admin.backups.section_restore"),
        t(lang, "admin.backups.last_restore", value=last_restore_value),
        t(lang, "admin.backups.last_restore_status", value=_backups_restore_status_label(str(overview.get("last_restore_status") or "never"), lang)),
    ]
    last_error = str(overview.get("last_error") or "").strip()
    if last_error:
        lines.extend(["", t(lang, "admin.backups.last_error", value=last_error)])
    return "\n".join(lines)


def _render_admin_backups_restore_page(lang: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    items = list_backups()
    if not items:
        return (
            f"{t(lang, 'admin.backups.list_title')}\n\n{t(lang, 'admin.backups.list_empty')}",
            InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "menu.back"), callback_data="menu:admin_backups")]]),
        )
    total = len(items)
    pages = max(1, (total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    chunk = items[page * LIST_PAGE_SIZE : (page + 1) * LIST_PAGE_SIZE]
    rows: List[List[InlineKeyboardButton]] = []
    for item in chunk:
        created_at = str(item.get("created_at") or "")
        created_label = _human_ago(created_at, lang) if created_at else str(item.get("name") or "")
        trigger_label = _backup_trigger_label(str(item.get("trigger") or ""), lang)
        token = backup_token(str(item.get("name") or ""))
        rows.append([InlineKeyboardButton(f"{created_label} · {trigger_label}", callback_data=f"menu:admin_backups_pick:{token}")])
    if pages > 1:
        nav: List[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"menu:admin_backups_restore:{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data=f"menu:admin_backups_restore:{page}"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"menu:admin_backups_restore:{page+1}"))
        rows.append(nav)
    rows.append([InlineKeyboardButton(t(lang, "menu.back"), callback_data="menu:admin_backups")])
    text = "\n".join([t(lang, "admin.backups.list_title"), "", t(lang, "admin.backups.list_intro")])
    return text, InlineKeyboardMarkup(rows)


def _render_admin_backups_restore_confirm(lang: str, token: str) -> tuple[str, InlineKeyboardMarkup]:
    resolved = resolve_backup_token(token)
    info = get_backup_info(str(resolved.get("name") or "")) if resolved else None
    if not info:
        return _render_admin_backups_restore_page(lang, 0)
    created_at = str(info.get("created_at") or "")
    created_value = _human_ago(created_at, lang) if created_at else str(info.get("name") or "")
    lines = [
        t(lang, "admin.backups.restore_confirm_title"),
        "",
        t(lang, "admin.backups.restore_confirm_created", value=created_value),
        t(lang, "admin.backups.restore_confirm_trigger", value=_backup_trigger_label(str(info.get("trigger") or ""), lang)),
        t(lang, "admin.backups.restore_confirm_size", value=_human_size(int(info.get("size_bytes") or 0))),
        t(lang, "admin.backups.restore_confirm_version", value=str(info.get("app_version") or "—")),
        "",
        t(lang, "admin.backups.restore_confirm_warning"),
    ]
    markup = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t(lang, "admin.backups.restore_action"), callback_data=f"menu:admin_backups_run_restore:{token}")],
            [InlineKeyboardButton(t(lang, "menu.back"), callback_data="menu:admin_backups_restore:0")],
        ]
    )
    return "\n".join(lines), markup


def admin_menu_text_router(update: Update, context: CallbackContext) -> None:
    if not _is_admin(update):
        return
    admin_settings_state = _admin_settings_state_get(context)
    if admin_settings_state and admin_settings_state.get("active") and admin_settings_state.get("step") in {"bot_title", "access_gate_message"}:
        lang = get_locale_for_update(update)
        title = (update.effective_message.text or "").strip()
        safe_delete_update_message(update, context)
        if not title:
            back_callback = "menu:admin_settings" if admin_settings_state.get("step") == "bot_title" else "menu:admin_settings_requests"
            if admin_settings_state.get("chat_id") and admin_settings_state.get("message_id"):
                safe_edit_by_ids(
                    context.bot,
                    int(admin_settings_state["chat_id"]),
                    int(admin_settings_state["message_id"]),
                    t(lang, "admin.settings.bot_title_empty") if admin_settings_state.get("step") == "bot_title" else t(lang, "admin.settings.access_gate_empty"),
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "menu.back"), callback_data=back_callback)]]),
                    parse_mode=PARSE_MODE,
                )
            _admin_settings_state_clear(context)
            return
        if admin_settings_state.get("step") == "bot_title":
            set_menu_title(title)
            saved_text = t(lang, "admin.settings.bot_title_saved")
            reply_text = _render_admin_settings_text(lang)
            reply_markup = kb_admin_settings_menu(
                _admin_notify_enabled(update.effective_user.id if update.effective_user else 0),
                is_global_telemetry_enabled(),
                are_access_requests_enabled(),
                lang,
            )
        else:
            set_access_gate_message(title)
            saved_text = t(lang, "admin.settings.access_gate_saved")
            reply_text = _render_admin_requests_settings_text(lang)
            reply_markup = kb_admin_requests_settings_menu(
                _admin_notify_enabled(update.effective_user.id if update.effective_user else 0),
                are_access_requests_enabled(),
                lang,
            )
        _admin_settings_state_clear(context)
        if admin_settings_state.get("chat_id") and admin_settings_state.get("message_id"):
            safe_edit_by_ids(
                context.bot,
                int(admin_settings_state["chat_id"]),
                int(admin_settings_state["message_id"]),
                f"{reply_text}\n\n{saved_text}",
                reply_markup=reply_markup,
                parse_mode=PARSE_MODE,
            )
        return
    announce_state = _announce_state_get(context)
    if announce_state and announce_state.get("active") and announce_state.get("step") == "compose":
        lang = get_locale_for_update(update)
        message_text = (update.effective_message.text or "").strip()
        safe_delete_update_message(update, context)
        if not message_text:
            _announce_edit(
                context,
                t(lang, "admin.announce.empty"),
                InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "menu.back"), callback_data="menu:admin")]]),
                parse_mode=PARSE_MODE,
            )
            _announce_state_clear(context)
            return
        announce_state["step"] = "confirm"
        announce_state["draft_text"] = message_text
        _announce_state_set(context, announce_state)
        _announce_edit(
            context,
            _announce_confirm_text(lang, message_text),
            _announce_confirm_markup(lang),
            parse_mode=None,
        )
        return
    state = _request_state_get(context)
    if not state or not state.get("active") or state.get("step") != "search":
        return
    lang = get_locale_for_update(update)
    query = (update.effective_message.text or "").strip().lower()
    users = user_store.read()
    matches: List[str] = []
    for user_id, rec in users.items():
        if not isinstance(rec, dict):
            continue
        if not rec.get("access_request_pending"):
            continue
        haystack = " ".join(
            [
                str(user_id),
                str(rec.get("username") or ""),
                str(rec.get("first_name") or ""),
                str(rec.get("last_name") or ""),
            ]
        ).lower()
        if query in haystack:
            matches.append(str(user_id))
    safe_delete_update_message(update, context)
    if not matches:
        state.update({"active": True, "step": "dashboard", "page": 0, "ids": []})
        _request_state_set(context, state)
        _request_edit(
            context,
            t(lang, "admin.requests.search_empty", query=_md(query)),
            InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "menu.back"), callback_data="menu:admin_requests")]]),
            parse_mode=PARSE_MODE,
        )
        return
    state.update({"active": True, "step": "dashboard", "page": 0, "ids": matches})
    _request_state_set(context, state)
    text, markup = _render_requests_dashboard(matches, 0, lang)
    _request_edit(context, text, markup, parse_mode=PARSE_MODE)


def on_menu_callback(update: Update, context: CallbackContext, payload: str) -> None:
    answer_cb(update)
    is_admin = _is_admin(update)
    user = update.effective_user
    lang = get_locale_for_update(update)

    if payload == "main":
        has_access = _has_access(update)
        if not has_access:
            text = _access_gate_text(user.id if user else 0, lang)
            safe_edit_message(
                update,
                context,
                text if not are_access_requests_enabled() else f"*{get_menu_title_markdown()}*\n\n{text}",
                reply_markup=kb_main_menu(False, False, lang, allow_requests=are_access_requests_enabled()),
                parse_mode=PARSE_MODE,
            )
            return
        safe_edit_message(
            update,
            context,
            f"*{get_menu_title_markdown()}*\n\n{t(lang, 'menu.choose_action')}",
            reply_markup=kb_main_menu(is_admin, has_access, lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "settings":
        _admin_settings_state_clear(context)
        telemetry_available = is_global_telemetry_enabled()
        safe_edit_message(
            update,
            context,
            t(lang, "settings.title"),
            reply_markup=kb_settings_menu(_user_telemetry_enabled(user.id if user else 0), telemetry_available, _user_announcement_silent(user.id if user else 0), lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "admin" and is_admin:
        _announce_state_clear(context)
        _request_state_clear(context)
        _admin_settings_state_clear(context)
        if should_show_initial_admin_setup():
            safe_edit_message(
                update,
                context,
                _render_admin_setup_text(lang),
                reply_markup=_render_admin_setup_markup(lang),
                parse_mode=PARSE_MODE,
            )
            return
        safe_edit_message(
            update,
            context,
            _render_admin_menu_text(lang),
            reply_markup=_admin_menu_markup(lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "admin_setup_later" and is_admin:
        set_initial_setup_state("dismissed")
        safe_edit_message(
            update,
            context,
            _render_admin_menu_text(lang),
            reply_markup=_admin_menu_markup(lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "language":
        safe_edit_message(
            update,
            context,
            t(lang, "language.title"),
            reply_markup=kb_language_menu(lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload.startswith("setlangstart:") and user:
        new_lang = set_user_locale(user.id, payload.split(":", 1)[1])
        context.user_data.pop("start_language_gate_pending", None)
        text, markup = _build_start_reply(
            update,
            new_lang,
            datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        )
        safe_edit_message(
            update,
            context,
            text,
            reply_markup=markup,
            parse_mode=PARSE_MODE,
        )
        return

    if payload.startswith("setlang:") and user:
        new_lang = set_user_locale(user.id, payload.split(":", 1)[1])
        if context.user_data.pop("start_language_gate_pending", False):
            text, markup = _build_start_reply(
                update,
                new_lang,
                datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            )
            safe_edit_message(
                update,
                context,
                text,
                reply_markup=markup,
                parse_mode=PARSE_MODE,
            )
            return
        safe_edit_message(
            update,
            context,
            t(new_lang, "language.title"),
            reply_markup=kb_language_menu(new_lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "settings_toggle_announce_sound" and user:
        silent = not _user_announcement_silent(user.id)
        _set_admin_flag(user.id, announcement_silent=silent)
        telemetry_available = is_global_telemetry_enabled()
        safe_edit_message(
            update,
            context,
            t(lang, "settings.title"),
            reply_markup=kb_settings_menu(_user_telemetry_enabled(user.id), telemetry_available, silent, lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "settings_toggle_telemetry" and user:
        if not is_global_telemetry_enabled():
            safe_edit_message(
                update,
                context,
                t(lang, "settings.title"),
                reply_markup=kb_settings_menu(False, False, _user_announcement_silent(user.id), lang),
                parse_mode=PARSE_MODE,
            )
            return
        enabled = not _user_telemetry_enabled(user.id)
        _set_admin_flag(user.id, telemetry_enabled=enabled)
        safe_edit_message(
            update,
            context,
            t(lang, "settings.title"),
            reply_markup=kb_settings_menu(enabled, True, _user_announcement_silent(user.id), lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "admin_announce" and is_admin:
        _announce_capture_message(update, context)
        _announce_state_set(context, {"active": True, "step": "compose", "draft_text": "", **(_announce_state_get(context) or {})})
        safe_edit_message(
            update,
            context,
            t(lang, "admin.announce.title"),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "menu.back"), callback_data="menu:admin")]]),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "admin_announce_edit" and is_admin:
        state = _announce_state_get(context) or {}
        state.update({"active": True, "step": "compose"})
        _announce_state_set(context, state)
        _announce_capture_message(update, context)
        safe_edit_message(
            update,
            context,
            t(lang, "admin.announce.title"),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "admin.announce.cancel"), callback_data="menu:admin_announce_cancel")]]),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "admin_announce_cancel" and is_admin:
        _announce_state_clear(context)
        safe_edit_message(
            update,
            context,
            t(lang, "admin.announce.cancelled"),
            reply_markup=_admin_menu_markup(lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "admin_announce_send" and is_admin:
        state = _announce_state_get(context) or {}
        draft_text = str(state.get("draft_text") or "").strip()
        if not draft_text:
            _announce_state_clear(context)
            safe_edit_message(
                update,
                context,
                t(lang, "admin.announce.empty"),
                reply_markup=_admin_menu_markup(lang),
                parse_mode=PARSE_MODE,
            )
            return
        users = user_store.read()
        sent = 0
        failed = 0
        sender_id = update.effective_user.id if update.effective_user else None
        for raw_user_id, rec in users.items():
            if not isinstance(rec, dict):
                continue
            chat_id = rec.get("chat_id")
            if not chat_id or not rec.get("access_granted"):
                continue
            try:
                target_user_id = int(raw_user_id)
            except (TypeError, ValueError):
                target_user_id = None
            if sender_id and target_user_id == sender_id:
                continue
            try:
                context.bot.send_message(
                    chat_id=int(chat_id),
                    text=draft_text,
                    disable_notification=bool(rec.get("announcement_silent", False)),
                )
                sent += 1
            except Exception:
                failed += 1
        _announce_state_clear(context)
        safe_edit_message(
            update,
            context,
            t(lang, "admin.announce.no_recipients") if sent == 0 and failed == 0 else t(lang, "admin.announce.sent", sent=sent, failed=failed),
            reply_markup=_admin_menu_markup(lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "admin_settings" and is_admin:
        _admin_settings_state_clear(context)
        safe_edit_message(
            update,
            context,
            _render_admin_settings_text(lang),
            reply_markup=kb_admin_settings_menu(
                _admin_notify_enabled(user.id if user else 0),
                is_global_telemetry_enabled(),
                are_access_requests_enabled(),
                lang,
            ),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "admin_settings_requests" and is_admin:
        _admin_settings_state_clear(context)
        safe_edit_message(
            update,
            context,
            _render_admin_requests_settings_text(lang),
            reply_markup=kb_admin_requests_settings_menu(
                _admin_notify_enabled(user.id if user else 0),
                are_access_requests_enabled(),
                lang,
            ),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "admin_settings_reset" and is_admin:
        _admin_settings_state_set(context, {"active": True, "step": "factory_reset"})
        safe_edit_message(
            update,
            context,
            _render_admin_reset_text(lang),
            reply_markup=_admin_reset_markup(lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload.startswith("admin_settings_reset_scope:") and is_admin:
        scope = payload.split(":", 1)[1]
        state = _admin_settings_state_get(context) or {}
        state.update({"active": True, "step": "factory_reset_confirm", "factory_reset_scope": scope})
        _admin_settings_state_set(context, state)
        safe_edit_message(
            update,
            context,
            _render_admin_reset_confirm_text(scope, lang),
            reply_markup=_admin_reset_confirm_markup(scope, lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload.startswith("admin_settings_reset_run:") and is_admin:
        scope = payload.split(":", 1)[1]
        cleanup_nodes = scope in {"nodes", "nodes_ssh"}
        safe_edit_message(
            update,
            context,
            t(lang, "admin.wizard.work_in_progress", title=t(lang, "admin.settings.reset_title"), dots=""),
            reply_markup=kb_back_to_admin(lang),
            parse_mode=PARSE_MODE,
        )
        rc, out = run_factory_reset(cleanup_nodes=cleanup_nodes, stop_local_runtime=(scope == "nodes_ssh"))
        _admin_settings_state_clear(context)
        safe_edit_message(
            update,
            context,
            f"{'✅' if rc == 0 else '⚠️'} {t(lang, 'admin.settings.reset_title')}\n\n{_md(out)}",
            reply_markup=kb_back_to_admin(lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "admin_updates" and is_admin:
        safe_edit_message(
            update,
            context,
            _render_admin_updates_text(lang),
            reply_markup=_admin_updates_markup(lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "admin_backups" and is_admin:
        safe_edit_message(
            update,
            context,
            _render_admin_backups_text(lang),
            reply_markup=kb_admin_backups_menu(lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "admin_backups_create" and is_admin:
        safe_edit_message(
            update,
            context,
            t(lang, "admin.backups.creating"),
            reply_markup=kb_admin_backups_menu(lang),
            parse_mode=PARSE_MODE,
        )
        result = create_backup("manual")
        text = _render_admin_backups_text(lang)
        if str(result.get("status")) == "skipped_duplicate":
            text = f"{text}\n\n{t(lang, 'admin.backups.duplicate_skipped')}"
        elif str(result.get("status")) == "failed":
            text = f"{text}\n\n{t(lang, 'admin.backups.last_error', value=str(result.get('message') or 'unknown error'))}"
        safe_edit_message(update, context, text, reply_markup=kb_admin_backups_menu(lang), parse_mode=PARSE_MODE)
        return

    if payload == "admin_backups_settings" and is_admin:
        safe_edit_message(
            update,
            context,
            t(lang, "admin.backups.settings_title"),
            reply_markup=kb_admin_backups_settings_menu(is_backups_enabled(), get_backups_interval_hours(), get_backups_keep_count(), lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "admin_backups_toggle" and is_admin:
        enabled = set_backups_enabled(not is_backups_enabled())
        safe_edit_message(
            update,
            context,
            t(lang, "admin.backups.settings_title"),
            reply_markup=kb_admin_backups_settings_menu(enabled, get_backups_interval_hours(), get_backups_keep_count(), lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload.startswith("admin_backups_interval:") and is_admin:
        raw = payload.split(":", 1)[1]
        hours = int(raw) if raw.isdigit() else get_backups_interval_hours()
        set_backups_interval_hours(hours)
        safe_edit_message(
            update,
            context,
            t(lang, "admin.backups.settings_title"),
            reply_markup=kb_admin_backups_settings_menu(is_backups_enabled(), get_backups_interval_hours(), get_backups_keep_count(), lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload.startswith("admin_backups_keep:") and is_admin:
        raw = payload.split(":", 1)[1]
        count = int(raw) if raw.isdigit() else get_backups_keep_count()
        set_backups_keep_count(count)
        safe_edit_message(
            update,
            context,
            t(lang, "admin.backups.settings_title"),
            reply_markup=kb_admin_backups_settings_menu(is_backups_enabled(), get_backups_interval_hours(), get_backups_keep_count(), lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload.startswith("admin_backups_restore:") and is_admin:
        raw_page = payload.split(":", 1)[1]
        page = int(raw_page) if raw_page.isdigit() else 0
        text, markup = _render_admin_backups_restore_page(lang, page)
        safe_edit_message(update, context, text, reply_markup=markup, parse_mode=PARSE_MODE)
        return

    if payload.startswith("admin_backups_pick:") and is_admin:
        token = payload.split(":", 1)[1]
        text, markup = _render_admin_backups_restore_confirm(lang, token)
        safe_edit_message(update, context, text, reply_markup=markup, parse_mode=PARSE_MODE)
        return

    if payload.startswith("admin_backups_run_restore:") and is_admin:
        token = payload.split(":", 1)[1]
        resolved = resolve_backup_token(token)
        safe_edit_message(
            update,
            context,
            t(lang, "admin.backups.restoring"),
            reply_markup=kb_admin_backups_menu(lang),
            parse_mode=PARSE_MODE,
        )
        result = restore_backup(str(resolved.get("name") or "")) if resolved else {"status": "failed", "message": "backup not found"}
        if str(result.get("status")) == "success":
            text = f"{_render_admin_backups_text(lang)}\n\n{t(lang, 'admin.backups.restore_done')}"
        else:
            text = f"{_render_admin_backups_text(lang)}\n\n{t(lang, 'admin.backups.restore_failed_text', value=str(result.get('message') or 'unknown error'))}"
        safe_edit_message(update, context, text, reply_markup=kb_admin_backups_menu(lang), parse_mode=PARSE_MODE)
        return

    if payload == "admin_updates_toggle_auto" and is_admin:
        enabled = set_updates_auto_check_enabled(not is_updates_auto_check_enabled())
        overview = get_updates_overview()
        update_running = str(overview.get("last_run_status") or "") == "running"
        show_update_action = bool(overview.get("update_supported")) and (bool(overview.get("update_available")) or update_running)
        safe_edit_message(
            update,
            context,
            _render_admin_updates_text(lang),
            reply_markup=kb_admin_updates_menu(enabled, show_update_action, update_running, str(overview.get("branch") or get_updates_branch()), lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "admin_updates_check" and is_admin:
        safe_edit_message(
            update,
            context,
            t(lang, "admin.updates.checking"),
            reply_markup=_admin_updates_markup(lang),
            parse_mode=PARSE_MODE,
        )
        check_for_updates(branch=get_updates_branch())
        safe_edit_message(
            update,
            context,
            _render_admin_updates_text(lang),
            reply_markup=_admin_updates_markup(lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "admin_updates_branch" and is_admin:
        safe_edit_message(
            update,
            context,
            _render_admin_updates_branch_text(lang),
            reply_markup=kb_admin_updates_branch_menu(get_updates_branch(), lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload.startswith("admin_updates_set_branch:") and is_admin:
        branch = payload.split(":", 1)[1].strip().lower()
        set_updates_branch(branch)
        check_for_updates(branch=branch)
        safe_edit_message(
            update,
            context,
            _render_admin_updates_text(lang),
            reply_markup=_admin_updates_markup(lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload.startswith("admin_updates_versions:") and is_admin:
        raw_page = payload.split(":", 1)[1]
        page = int(raw_page) if raw_page.isdigit() else 0
        text, markup = _render_admin_updates_versions_page(lang, page)
        safe_edit_message(update, context, text, reply_markup=markup, parse_mode=PARSE_MODE)
        return

    if payload.startswith("admin_updates_version:") and is_admin:
        ref = payload.split(":", 1)[1]
        text, markup = _render_admin_updates_version_confirm(lang, ref)
        safe_edit_message(update, context, text, reply_markup=markup, parse_mode=PARSE_MODE)
        return

    if payload == "admin_updates_run" and is_admin:
        overview = get_updates_overview()
        if str(overview.get("last_run_status") or "") == "running":
            safe_edit_message(
                update,
                context,
                _render_admin_updates_text(lang, include_failure_log=False),
                reply_markup=_admin_updates_markup(lang),
                parse_mode=PARSE_MODE,
            )
            return
        if not overview.get("update_available"):
            safe_edit_message(
                update,
                context,
                _render_admin_updates_text(lang),
                reply_markup=_admin_updates_markup(lang),
                parse_mode=PARSE_MODE,
            )
            return
        else:
            versions = list_available_versions(get_updates_branch()).get("versions") or []
            latest_ref = next((str(item.get("ref")) for item in versions if item.get("action") == "upgrade"), "")
            if not latest_ref:
                safe_edit_message(
                    update,
                    context,
                    _render_admin_updates_text(lang),
                    reply_markup=_admin_updates_markup(lang),
                    parse_mode=PARSE_MODE,
                )
                return
            safe_edit_message(
                update,
                context,
                t(lang, "admin.updates.starting"),
                reply_markup=_admin_updates_markup(lang),
                parse_mode=PARSE_MODE,
            )
        schedule_update(branch=get_updates_branch(), target_ref=latest_ref)
        safe_edit_message(
            update,
            context,
            _render_admin_updates_text(lang, include_failure_log=False),
            reply_markup=_admin_updates_markup(lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload.startswith("admin_updates_install:") and is_admin:
        ref = payload.split(":", 1)[1]
        text, markup = _render_admin_updates_version_confirm(lang, ref)
        safe_edit_message(
            update,
            context,
            t(lang, "admin.updates.starting"),
            reply_markup=markup,
            parse_mode=PARSE_MODE,
        )
        schedule_update(branch=get_updates_branch(), target_ref=ref)
        safe_edit_message(
            update,
            context,
            _render_admin_updates_text(lang, include_failure_log=False),
            reply_markup=_admin_updates_markup(lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "admin_settings_toggle_notify" and is_admin and user:
        enabled = not _admin_notify_enabled(user.id)
        _set_admin_flag(user.id, notify_access_requests=enabled)
        safe_edit_message(
            update,
            context,
            _render_admin_requests_settings_text(lang),
            reply_markup=kb_admin_requests_settings_menu(enabled, are_access_requests_enabled(), lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "admin_settings_bot_title" and is_admin:
        _admin_settings_capture_message(update, context)
        _admin_settings_state_set(context, {"active": True, "step": "bot_title", **(_admin_settings_state_get(context) or {})})
        safe_edit_message(
            update,
            context,
            f"{t(lang, 'admin.settings.bot_title_prompt')}\n\n{t(lang, 'admin.settings.current_title', title=get_menu_title_markdown())}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "menu.back"), callback_data="menu:admin_settings")]]),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "admin_settings_access_gate_message" and is_admin:
        _admin_settings_capture_message(update, context)
        _admin_settings_state_set(context, {"active": True, "step": "access_gate_message", **(_admin_settings_state_get(context) or {})})
        safe_edit_message(
            update,
            context,
            f"{t(lang, 'admin.settings.access_gate_prompt')}\n\n{t(lang, 'admin.settings.current_access_gate', text=_md(get_access_gate_message()))}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "menu.back"), callback_data="menu:admin_settings_requests")]]),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "admin_settings_toggle_telemetry" and is_admin:
        enabled = set_global_telemetry_enabled(not is_global_telemetry_enabled())
        safe_edit_message(
            update,
            context,
            _render_admin_settings_text(lang),
            reply_markup=kb_admin_settings_menu(_admin_notify_enabled(user.id if user else 0), enabled, are_access_requests_enabled(), lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "admin_settings_toggle_requests" and is_admin:
        enabled = set_access_requests_enabled(not are_access_requests_enabled())
        safe_edit_message(
            update,
            context,
            _render_admin_requests_settings_text(lang),
            reply_markup=kb_admin_requests_settings_menu(_admin_notify_enabled(user.id if user else 0), enabled, lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "admin_requests" and is_admin:
        _open_requests_dashboard(update, context, lang)
        return

    if payload == "admin_requests_search" and is_admin:
        _request_capture_message(update, context)
        state = _request_state_get(context) or {}
        state.update({"active": True, "step": "search"})
        _request_state_set(context, state)
        safe_edit_message(
            update,
            context,
            t(lang, "admin.requests.search_title"),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "menu.back"), callback_data="menu:admin_requests")]]),
            parse_mode=PARSE_MODE,
        )
        return

    if payload.startswith("admin_requests_page:") and is_admin:
        page = int(payload.split(":", 1)[1])
        state = _request_state_get(context) or {}
        ids = state.get("ids") if isinstance(state.get("ids"), list) else _all_pending_request_ids()
        _open_requests_dashboard(update, context, lang, page=page, ids=ids)
        return

    if payload.startswith("admin_request_card:") and is_admin:
        _request_capture_message(update, context)
        user_id = payload.rsplit(":", 1)[-1]
        state = _request_state_get(context) or {}
        ids = state.get("ids") if isinstance(state.get("ids"), list) else _all_pending_request_ids()
        state.update({"active": True, "step": "card", "ids": ids, "selected_user_id": user_id})
        _request_state_set(context, state)
        text, markup = _render_request_card(user_id, lang)
        safe_edit_message(update, context, text, reply_markup=markup, parse_mode=PARSE_MODE)
        return

    if payload.startswith("admin_request_approve:") and is_admin:
        req_user_id = int(payload.rsplit(":", 1)[-1])
        profile_name = _ensure_profile_for_request(req_user_id)
        _set_admin_flag(req_user_id, access_granted=True, access_request_pending=False, profile_name=profile_name)
        try:
            context.bot.send_message(chat_id=req_user_id, text=t(get_user_locale(req_user_id), "admin.requests.notify_approved"))
        except Exception:
            pass
        text, markup = _render_request_card(str(req_user_id), lang)
        safe_edit_message(
            update,
            context,
            f"{t(lang, 'admin.requests.approved_with_profile')}\n{t(lang, 'admin.requests.profile_created', name=_md(profile_name))}\n\n{text}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton(t(lang, "admin.requests.setup_now"), callback_data=f"cfg:quickedit:{profile_name}")],
                    [InlineKeyboardButton(t(lang, "admin.requests.setup_later"), callback_data="menu:admin_requests")],
                ]
            ),
            parse_mode=PARSE_MODE,
        )
        return

    if payload.startswith("admin_request_reject:") and is_admin:
        req_user_id = int(payload.rsplit(":", 1)[-1])
        _set_admin_flag(req_user_id, access_granted=False, access_request_pending=False)
        try:
            context.bot.send_message(chat_id=req_user_id, text=t(get_user_locale(req_user_id), "admin.requests.notify_rejected"))
        except Exception:
            pass
        text, markup = _render_request_card(str(req_user_id), lang)
        safe_edit_message(update, context, f"{t(lang, 'admin.requests.rejected_admin')}\n\n{text}", reply_markup=markup, parse_mode=PARSE_MODE)
        return

    if payload == "request_access" and user:
        if not are_access_requests_enabled():
            safe_edit_message(
                update,
                context,
                get_access_gate_message(),
                reply_markup=kb_main_menu(False, False, lang, allow_requests=False),
                parse_mode=PARSE_MODE,
            )
            return
        db = user_store.read()
        rec = db.get(str(user.id)) if isinstance(db, dict) else None
        if isinstance(rec, dict) and rec.get("access_request_pending"):
            safe_edit_message(
                update,
                context,
                t(lang, "access.pending"),
                reply_markup=kb_main_menu(False, False, lang, allow_requests=True),
                parse_mode=PARSE_MODE,
            )
            return

        def mut(users_db):
            item = users_db.get(str(user.id)) if isinstance(users_db.get(str(user.id)), dict) else {}
            item["chat_id"] = update.effective_chat.id if update.effective_chat else item.get("chat_id")
            item["username"] = user.username or ""
            item["first_name"] = user.first_name or ""
            item["last_name"] = user.last_name or ""
            item["locale"] = item.get("locale") or lang
            item["access_request_pending"] = True
            item["access_granted"] = False
            item["access_request_sent_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            item["notify_access_requests"] = bool(item.get("notify_access_requests", True))
            item["announcement_silent"] = bool(item.get("announcement_silent", False))
            item["telemetry_enabled"] = bool(item.get("telemetry_enabled", False))
            users_db[str(user.id)] = item
            return users_db

        user_store.update(mut)

        username_text = f"@{user.username}" if user.username else "—"
        full_name = " ".join(part for part in [(user.first_name or "").strip(), (user.last_name or "").strip()] if part) or "—"
        for admin_id in ADMIN_IDS:
            if not _admin_notify_enabled(admin_id):
                continue
            try:
                admin_lang = get_user_locale(admin_id)
                context.bot.send_message(
                    chat_id=admin_id,
                    text=t(admin_lang, "admin.requests.notify_new", user_id=_md(user.id), username=_md(username_text), name=_md(full_name)),
                    parse_mode=PARSE_MODE,
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(t(admin_lang, "admin.requests.approve"), callback_data=f"menu:admin_request_approve:{user.id}"),
                                InlineKeyboardButton(t(admin_lang, "admin.requests.reject"), callback_data=f"menu:admin_request_reject:{user.id}"),
                            ],
                            [InlineKeyboardButton(t(admin_lang, "menu.requests"), callback_data="menu:admin_requests")],
                        ]
                    ),
                )
            except Exception:
                pass

        safe_edit_message(
            update,
            context,
            t(lang, "access.request_sent"),
            reply_markup=kb_main_menu(False, False, lang, allow_requests=True),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "profile":
        if not _has_access(update):
            gate = _access_gate_text(user.id if user else 0, lang)
            safe_edit_message(update, context, gate, reply_markup=kb_main_menu(False, False, lang, allow_requests=are_access_requests_enabled()), parse_mode=PARSE_MODE)
            return
        profile_name = _resolve_profile_name(user.id if user else None)
        if not user or not profile_name:
            safe_edit_message(
                update,
                context,
                f"{t(lang, 'profile.title')}\n\n{t(lang, 'admin.requests.profile_missing')}",
                reply_markup=kb_profile_minimal(lang),
                parse_mode=PARSE_MODE,
            )
            return

        name = profile_name
        prof = get_profile(name)
        st = get_profile_access_status(name)
        allowed = get_allowed_protocols(name)
        server_access = format_server_access(name, allowed, list_awg_server_keys(name), lang)
        username_text = _format_username(str(user.username or ""), lang)

        status_line = t(lang, "status.inactive")
        if prof and st.get("frozen"):
            status_line = t(lang, "status.frozen")
        elif prof and st.get("active"):
            status_line = t(lang, "status.active")

        safe_edit_message(
            update,
            context,
            (
                f"{t(lang, 'profile.title')}\n\n"
                f"{t(lang, 'profile.name', name=name)}\n"
                f"{t(lang, 'profile.status', status=status_line)}\n\n"
                f"{t(lang, 'profile.identity')}\n"
                f"{t(lang, 'profile.telegram_id', value=user.id)}\n"
                f"{t(lang, 'profile.username', value=username_text)}\n\n"
                f"{t(lang, 'profile.access_section')}\n"
                f"{server_access}"
            ),
            reply_markup=kb_profile_minimal(lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "profile_stats":
        if not _has_access(update):
            gate = _access_gate_text(user.id if user else 0, lang)
            safe_edit_message(update, context, gate, reply_markup=kb_main_menu(False, False, lang, allow_requests=are_access_requests_enabled()), parse_mode=PARSE_MODE)
            return
        profile_name = _resolve_profile_name(user.id if user else None)
        if not user or not profile_name:
            safe_edit_message(
                update,
                context,
                t(lang, "admin.requests.profile_missing"),
                reply_markup=kb_profile_minimal(lang),
                parse_mode=PARSE_MODE,
            )
            return

        name = profile_name
        st = get_profile_access_status(name)
        prof = get_profile(name) or {}
        allowed = get_allowed_protocols(name)
        methods = get_access_methods_for_codes(allowed)
        status_line = t(lang, "status.inactive")
        if prof and st.get("frozen"):
            status_line = t(lang, "status.frozen")
        elif prof and st.get("active"):
            status_line = t(lang, "status.active")

        created_at = prof.get("created_at") if isinstance(prof, dict) else None
        uuid_val = prof.get("uuid") if isinstance(prof, dict) else None
        frozen_flag = t(lang, "profile.frozen_yes") if st.get("frozen") else t(lang, "profile.frozen_no")

        awg_server_keys = list_awg_server_keys(name)
        server_access = format_server_access(name, allowed, awg_server_keys, lang)
        server_count = len({method.server_key for method in methods})
        xray_count = len([method for method in methods if method.protocol_kind == "xray"])
        awg_count = len([method for method in methods if method.protocol_kind == "awg"])

        u_db = user_store.read()
        u_rec = u_db.get(str(user.id)) if isinstance(u_db, dict) else None
        last_key_at = u_rec.get("last_key_at") if isinstance(u_rec, dict) else None
        key_cnt = u_rec.get("key_issued_count") if isinstance(u_rec, dict) else 0
        last_key_txt = _human_ago(last_key_at, lang) if last_key_at else "—"
        username_text = _format_username(str(user.username or ""), lang)
        created_txt = _human_ago(created_at, lang) if created_at else "—"
        traffic_block = ""
        if is_global_telemetry_enabled():
            if _user_telemetry_enabled(user.id):
                awg_usage = get_profile_monthly_usage(name, "awg")
                xray_usage = get_profile_monthly_usage(name, "xray")
                awg_usage_txt = _format_bytes(int(awg_usage["total_bytes"]))
                xray_usage_txt = _format_bytes(int(xray_usage["total_bytes"]))
                telemetry_line = (
                    f"{t(lang, 'profile.awg_traffic', value=awg_usage_txt)}\n"
                    f"{t(lang, 'profile.xray_traffic', value=xray_usage_txt)}"
                )
            else:
                telemetry_line = t(lang, "profile.telemetry_disabled_user")
            traffic_block = f"{t(lang, 'profile.traffic')}\n{telemetry_line}\n\n"

        safe_edit_message(
            update,
            context,
            (
                f"{t(lang, 'profile.stats_title')}\n"
                "\n"
                f"👤 `{name}`\n"
                f"{t(lang, 'profile.status', status=status_line)}\n\n"
                f"{t(lang, 'profile.identity')}\n"
                f"{t(lang, 'profile.telegram_id', value=user.id)}\n"
                f"{t(lang, 'profile.username', value=username_text)}\n"
                f"{t(lang, 'profile.member_since', value=created_txt)}\n\n"
                f"{t(lang, 'profile.coverage')}\n"
                f"{t(lang, 'profile.servers_count', count=server_count)}\n"
                f"{t(lang, 'profile.protocols_count', count=len(methods))}\n"
                f"{t(lang, 'profile.xray_count', count=xray_count)}\n"
                f"{t(lang, 'profile.awg_count', count=awg_count)}\n\n"
                f"{t(lang, 'profile.access_section')}\n"
                f"{server_access}\n"
                + f"{t(lang, 'profile.frozen', value=frozen_flag)}\n\n"
                f"{traffic_block}"
                f"{t(lang, 'profile.activity')}\n"
                f"{t(lang, 'profile.keys_issued', count=key_cnt)}\n"
                f"{t(lang, 'profile.last_key', value=last_key_txt)}\n\n"
            ),
            reply_markup=kb_profile_stats(is_admin, lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "sshkey" and is_admin:
        ok, text = render_public_key_summary(lang)
        if not ok:
            safe_edit_message(
                update,
                context,
                t(lang, "ssh.error_setup", error=text[-1500:]),
                reply_markup=kb_back_to_admin(lang),
                parse_mode=None,
            )
            return
        safe_edit_message(
            update,
            context,
            text[:3900],
            reply_markup=_ssh_key_summary_markup(lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "sshkey_details" and is_admin:
        ok, text = render_public_key_guide(lang)
        if not ok:
            safe_edit_message(
                update,
                context,
                t(lang, "ssh.error_setup", error=text[-1500:]),
                reply_markup=kb_back_to_admin(lang),
                parse_mode=None,
            )
            return
        safe_edit_message(
            update,
            context,
            text[:3900],
            reply_markup=_ssh_key_details_markup(lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "admin_status" and is_admin:
        safe_edit_message(
            update,
            context,
            _render_admin_status(lang),
            reply_markup=_kb_admin_status(lang),
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "admin_problem_servers" and is_admin:
        text, markup = _render_problem_servers(lang)
        safe_edit_message(
            update,
            context,
            text,
            reply_markup=markup,
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "admin_runtime_sync_all" and is_admin:
        text, markup = _render_runtime_sync_confirm(lang)
        safe_edit_message(
            update,
            context,
            text,
            reply_markup=markup,
            parse_mode=PARSE_MODE,
        )
        return

    if payload == "admin_runtime_sync_run" and is_admin:
        safe_edit_message(
            update,
            context,
            t(lang, "admin.status.runtime_sync_running"),
            reply_markup=_kb_admin_status(lang),
            parse_mode=PARSE_MODE,
        )
        safe_edit_message(
            update,
            context,
            _sync_runtime_drift(lang),
            reply_markup=_kb_admin_status(lang),
            parse_mode=PARSE_MODE,
        )
        return

    safe_edit_message(
        update,
        context,
        f"*{get_menu_title_markdown()}*\n\n{t(lang, 'menu.choose_action')}",
        reply_markup=kb_main_menu(is_admin, _has_access(update), lang),
        parse_mode=PARSE_MODE,
    )
