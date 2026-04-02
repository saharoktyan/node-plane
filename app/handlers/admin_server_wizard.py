from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Sequence, Set

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext

from config import CB_MENU, CB_SRV, PARSE_MODE
from i18n import get_locale_for_update, t
from services.provisioning_state import (
    reconcile_server_state,
    reconcile_xray_server_state,
    render_server_provisioning_summary,
    summarize_server_provisioning,
)
from services.server_bootstrap import (
    bootstrap_server,
    check_server_ports,
    delete_server_runtime,
    full_cleanup_server,
    install_server_docker,
    is_server_docker_available,
    open_server_ports,
    probe_server,
    regenerate_awg_entropy,
    reinstall_server,
    show_server_metrics,
    show_awg_entropy,
    sync_server_node_env,
    sync_xray_server_settings,
)
from services.app_settings import set_initial_setup_state
from services.server_registry import RegisteredServer, get_server, list_servers, update_server_fields, upsert_server
from services.xray import get_server_link_status
from utils.tg import answer_cb, safe_delete_by_id, safe_delete_update_message, safe_edit_by_ids, safe_edit_message
from utils.security import validate_server_field, validate_server_key

from .admin_common import guard, kb_back_menu


def _md(value: Any) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("*", "\\*")
        .replace("_", "\\_")
        .replace("[", "\\[")
    )


def _wizard_get(context: CallbackContext) -> Optional[Dict[str, Any]]:
    w = context.user_data.get("server_wizard")
    return w if isinstance(w, dict) else None


def _wizard_set(context: CallbackContext, w: Dict[str, Any]) -> None:
    context.user_data["server_wizard"] = w


def _wizard_clear(context: CallbackContext) -> None:
    context.user_data.pop("server_wizard", None)


def _wizard_init(sent_message, mode: str) -> Dict[str, Any]:
    return {
        "active": True,
        "mode": mode,
        "step": "menu" if mode == "menu" else "key",
        "chat_id": sent_message.chat_id,
        "message_id": sent_message.message_id,
        "server_key": None,
        "data": {
            "key": "",
            "title": "",
            "flag": "🏳️",
            "region": "",
            "transport": "ssh",
            "target": "",
            "public_host": "",
            "notes": "",
            "protocol_kinds": set(),
            "awg_i1_preset": "quic",
        },
        "locale": "ru",
    }


def _wizard_lang(context: CallbackContext) -> str:
    w = _wizard_get(context)
    return str(w.get("locale") or "ru") if w else "ru"


def _wizard_edit(context: CallbackContext, text: str, markup: InlineKeyboardMarkup) -> None:
    w = _wizard_get(context)
    if not w:
        return
    safe_edit_by_ids(context.bot, int(w["chat_id"]), int(w["message_id"]), text, markup, parse_mode=None)


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


def _servers_menu_text(lang: str) -> str:
    return f"{t(lang, 'admin.menu_title')}\n\n{t(lang, 'menu.admin_choose')}"


def _step_nav_markup(
    lang: str,
    *,
    show_back: bool = True,
    next_payload: str | None = None,
    back_payload: str | None = None,
    next_label_key: str = "admin.wizard.next",
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    nav_row: list[InlineKeyboardButton] = []
    if show_back:
        nav_row.append(InlineKeyboardButton(t(lang, "menu.back"), callback_data=back_payload or f"{CB_SRV}back"))
    if next_payload:
        nav_row.append(InlineKeyboardButton(t(lang, next_label_key), callback_data=next_payload))
    if nav_row:
        rows.append(nav_row)
    return InlineKeyboardMarkup(rows)


def _server_menu_markup(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t(lang, "admin.wizard.new_server"), callback_data=f"{CB_SRV}start:create")],
            [InlineKeyboardButton(t(lang, "admin.wizard.edit_server"), callback_data=f"{CB_SRV}start:edit")],
            [InlineKeyboardButton(t(lang, "menu.back"), callback_data=f"{CB_MENU}admin")],
        ]
    )


def _transport_markup(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("local", callback_data=f"{CB_SRV}transport:local")],
            [InlineKeyboardButton("ssh", callback_data=f"{CB_SRV}transport:ssh")],
            [
                InlineKeyboardButton(t(lang, "menu.back"), callback_data=f"{CB_SRV}back"),
                InlineKeyboardButton(t(lang, "admin.wizard.next"), callback_data=f"{CB_SRV}next"),
            ],
        ]
    )


def _protocol_markup(selected: Set[str], lang: str) -> InlineKeyboardMarkup:
    def mark(code: str, label: str) -> str:
        return ("✅ " if code in selected else "⬜ ") + label

    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(mark("xray", "Xray"), callback_data=f"{CB_SRV}protocol:xray")],
            [InlineKeyboardButton(mark("awg", "AWG"), callback_data=f"{CB_SRV}protocol:awg")],
            [
                InlineKeyboardButton(t(lang, "menu.back"), callback_data=f"{CB_SRV}back"),
                InlineKeyboardButton(t(lang, "admin.wizard.next"), callback_data=f"{CB_SRV}protocol:done"),
            ],
        ]
    )


def _awg_preset_markup(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("QUIC", callback_data=f"{CB_SRV}awgpreset:quic")],
            [InlineKeyboardButton("DNS", callback_data=f"{CB_SRV}awgpreset:dns")],
            [InlineKeyboardButton("Chaos", callback_data=f"{CB_SRV}awgpreset:chaos")],
            [InlineKeyboardButton(t(lang, "menu.back"), callback_data=f"{CB_SRV}back")],
        ]
    )


def _pick_server_markup(servers: Sequence[RegisteredServer], lang: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"{server.flag} {server.title} ({server.key})", callback_data=f"{CB_SRV}pick:{server.key}")]
        for server in servers
    ]
    rows.append([InlineKeyboardButton(t(lang, "menu.back"), callback_data=f"{CB_SRV}list")])
    return InlineKeyboardMarkup(rows)


def _server_status(server: RegisteredServer, lang: str) -> tuple[str, str]:
    if server.bootstrap_state == "bootstrapped":
        return "✅", t(lang, "admin.wizard.server_status_ready")
    if "failed" in server.bootstrap_state:
        return "⚠️", t(lang, "admin.wizard.server_status_error")
    if server.bootstrap_state in {"edited", "new"}:
        return "🛠", t(lang, "admin.wizard.server_status_bootstrap")
    return "•", server.bootstrap_state or "unknown"


def _xray_status(server: RegisteredServer, lang: str) -> tuple[str, str]:
    if "xray" not in server.protocol_kinds:
        return "—", t(lang, "admin.wizard.server_status_disabled")
    if server.bootstrap_state != "bootstrapped":
        if "failed" in server.bootstrap_state:
            return "⚠️", t(lang, "admin.wizard.server_status_error")
        return "🛠", t(lang, "admin.wizard.server_status_bootstrap")
    ready, reason = get_server_link_status(server.key)
    if ready:
        return "✅", t(lang, "admin.wizard.server_status_ready")
    if "incomplete" in reason:
        return "⚠️", t(lang, "admin.wizard.server_status_link_incomplete")
    return "⚠️", reason


def _awg_status(server: RegisteredServer, lang: str) -> tuple[str, str]:
    if "awg" not in server.protocol_kinds:
        return "—", t(lang, "admin.wizard.server_status_disabled")
    if server.bootstrap_state == "bootstrapped":
        return "✅", t(lang, "admin.wizard.server_status_awg_runtime")
    if "failed" in server.bootstrap_state:
        return "⚠️", t(lang, "admin.wizard.server_status_awg_failed")
    return "🛠", t(lang, "admin.wizard.server_status_bootstrap")


def _server_dashboard_text(servers: Sequence[RegisteredServer], lang: str) -> str:
    lines = [t(lang, "admin.wizard.server_menu"), ""]
    for server in servers:
        status_icon, status_text = _server_overall_status(server, lang)
        prov_summary = summarize_server_provisioning(server.key)
        total = int(prov_summary["total"])
        prov_suffix = ""
        if total > 0:
            failed = int(prov_summary["by_status"]["failed"])
            attention = int(prov_summary["by_status"]["needs_attention"])
            ready = int(prov_summary["by_status"]["provisioned"])
            if lang == "ru":
                prov_suffix = f" | профили {ready}/{total}"
            else:
                prov_suffix = f" | profiles {ready}/{total}"
            if failed > 0:
                prov_suffix += f" | {'ошибки' if lang == 'ru' else 'failed'} {failed}"
            elif attention > 0:
                prov_suffix += f" | {'внимание' if lang == 'ru' else 'attention'} {attention}"
        lines.append(f"{server.flag} {server.title} ({server.key})")
        lines.append(f"• {status_icon} {status_text}{prov_suffix}")
        lines.append("")
    return "\n".join(line for line in lines).rstrip()


def _server_overall_status(server: RegisteredServer, lang: str) -> tuple[str, str]:
    server_icon, server_text = _server_status(server, lang)
    if server_icon == "⚠️":
        return server_icon, server_text
    if server.bootstrap_state != "bootstrapped":
        return server_icon, server_text

    prov = summarize_server_provisioning(server.key)
    if prov["overall"] == "failed":
        return "⚠️", t(lang, "admin.wizard.server_status_attention")
    if prov["overall"] == "needs_attention":
        return "⚠️", t(lang, "admin.wizard.server_status_attention")

    xray_ready, _ = get_server_link_status(server.key) if "xray" in server.protocol_kinds else (True, "ok")
    awg_ready = ("awg" not in server.protocol_kinds) or server.bootstrap_state == "bootstrapped"
    if xray_ready and awg_ready:
        return "✅", t(lang, "admin.wizard.server_status_ready")
    return "⚠️", t(lang, "admin.wizard.server_status_attention")


def _server_dashboard_markup(servers: Sequence[RegisteredServer], lang: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"{server.flag} {server.title}", callback_data=f"{CB_SRV}card:{server.key}")]
        for server in servers
    ]
    rows.append([InlineKeyboardButton(t(lang, "admin.wizard.new_server"), callback_data=f"{CB_SRV}start:create")])
    rows.append([InlineKeyboardButton(t(lang, "menu.back"), callback_data=f"{CB_MENU}admin")])
    return InlineKeyboardMarkup(rows)


def _advanced_section_for_field(field: str) -> str:
    if field in {"xray_host", "xray_sni", "xray_fp"}:
        return "xray"
    if field in {"awg_public_host", "awg_iface", "awg_i1_preset"}:
        return "awg"
    return "general"


def _server_recommended_actions(server: RegisteredServer, lang: str) -> list[str]:
    items: list[str] = []
    if not server.enabled:
        return items
    if server.bootstrap_state != "bootstrapped":
        items.append(t(lang, "admin.wizard.server_action_bootstrap"))
        return items
    prov = summarize_server_provisioning(server.key)
    if prov["overall"] in {"failed", "needs_attention"}:
        items.append(t(lang, "admin.wizard.server_action_provisioning"))
    if "xray" in server.protocol_kinds:
        ready, reason = get_server_link_status(server.key)
        if not ready:
            if "incomplete" in reason:
                items.append(t(lang, "admin.wizard.server_action_xray_link"))
            else:
                items.append(t(lang, "admin.wizard.server_action_xray_check"))
    return items


def _format_server_notes(notes: str, lang: str) -> str:
    raw = str(notes or "").strip()
    if not raw:
        return ""
    if raw.startswith("probe: "):
        parts = [part.strip() for part in raw[len("probe: ") :].split("|") if part.strip()]
        lines = [t(lang, "admin.wizard.server_card_note_probe")]
        for part in parts:
            lines.append(f"• {_localize_action_output(part, lang)}")
        return "\n".join(lines)
    return _localize_action_output(raw, lang)


def _server_card_text(server: RegisteredServer, lang: str) -> str:
    server_icon, server_text = _server_status(server, lang)
    xray_icon, xray_text = _xray_status(server, lang)
    awg_icon, awg_text = _awg_status(server, lang)
    overall_icon, overall_text = _server_overall_status(server, lang)
    prov_summary = summarize_server_provisioning(server.key)
    protocols = ", ".join(server.protocol_kinds) or "—"
    actions = _server_recommended_actions(server, lang)
    lines = [
        f"🖥 {server.flag} {server.title} ({server.key})",
        f"• {overall_icon} {overall_text}",
        "",
        t(lang, "admin.wizard.server_card_summary"),
        t(lang, "admin.wizard.server_card_infra", icon=server_icon, status=server_text),
        t(lang, "admin.wizard.server_card_transport", value=server.transport),
        t(lang, "admin.wizard.server_card_protocols", value=protocols),
        t(lang, "admin.wizard.server_card_host", value=server.public_host or "—"),
        "",
        t(lang, "admin.wizard.server_card_runtime"),
        t(lang, "admin.wizard.server_card_xray", icon=xray_icon, status=xray_text, tcp=server.xray_tcp_port, xhttp=server.xray_xhttp_port),
        t(lang, "admin.wizard.server_card_awg", icon=awg_icon, status=awg_text, port=server.awg_port, iface=server.awg_iface),
        t(
            lang,
            "admin.wizard.server_card_provisioning_line",
            ready=int(prov_summary["by_status"]["provisioned"]),
            total=int(prov_summary["total"]),
            failed=int(prov_summary["by_status"]["failed"]),
            attention=int(prov_summary["by_status"]["needs_attention"]),
        ),
    ]
    if actions:
        lines.extend(["", t(lang, "admin.wizard.server_card_actions")])
        lines.extend([f"• {item}" for item in actions[:3]])
    else:
        lines.extend(["", t(lang, "admin.wizard.server_card_actions"), f"• {t(lang, 'admin.wizard.server_card_no_actions')}"])
    if server.notes:
        lines.extend(["", t(lang, "admin.wizard.server_card_notes"), _format_server_notes(server.notes, lang)])
    return "\n".join(lines)


def _server_card_markup(server_key: str, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(t(lang, "admin.wizard.probe"), callback_data=f"{CB_SRV}action:probe:{server_key}"),
                InlineKeyboardButton(t(lang, "admin.wizard.bootstrap"), callback_data=f"{CB_SRV}bootmenu:{server_key}"),
            ],
            [InlineKeyboardButton(t(lang, "admin.wizard.advanced"), callback_data=f"{CB_SRV}advanced:{server_key}")],
            [InlineKeyboardButton(t(lang, "admin.wizard.to_servers"), callback_data=f"{CB_SRV}list")],
        ]
    )


def _probe_result_markup(server_key: str, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t(lang, "admin.wizard.back_to_server"), callback_data=f"{CB_SRV}card:{server_key}")],
        ]
    )


def _metrics_result_markup(server_key: str, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t(lang, "admin.wizard.back_to_maintenance"), callback_data=f"{CB_SRV}advsection:maintenance:{server_key}")],
        ]
    )


def _bootstrap_menu_text(server: RegisteredServer, lang: str) -> str:
    if not is_server_docker_available(server.key):
        state = t(lang, "admin.wizard.bootstrap_menu_state_docker_missing")
    elif server.bootstrap_state == "bootstrapped":
        state = t(lang, "admin.wizard.server_status_ready")
    else:
        state = t(lang, "admin.wizard.server_status_bootstrap")
    return "\n".join(
        [
            f"🛠 {server.flag} {server.title} ({server.key})",
            "",
            t(lang, "admin.wizard.bootstrap_menu_title"),
            t(lang, "admin.wizard.bootstrap_menu_state", state=state),
            "",
            t(lang, "admin.wizard.bootstrap_menu_intro"),
        ]
    )


def _bootstrap_menu_markup(server: RegisteredServer, lang: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if not is_server_docker_available(server.key):
        rows.append([InlineKeyboardButton(t(lang, "admin.wizard.install_docker"), callback_data=f"{CB_SRV}action:installdocker:{server.key}")])
        rows.append([InlineKeyboardButton(t(lang, "admin.wizard.back_to_server"), callback_data=f"{CB_SRV}card:{server.key}")])
        return InlineKeyboardMarkup(rows)
    if server.bootstrap_state == "bootstrapped":
        rows.append(
            [
                InlineKeyboardButton(t(lang, "admin.wizard.reinstall"), callback_data=f"{CB_SRV}bootmode:reinstall:{server.key}"),
                InlineKeyboardButton(t(lang, "admin.wizard.delete_runtime"), callback_data=f"{CB_SRV}bootmode:delete:{server.key}"),
            ],
        )
    else:
        rows.append([InlineKeyboardButton(t(lang, "admin.wizard.bootstrap"), callback_data=f"{CB_SRV}bootmode:bootstrap:{server.key}")])
    rows.append([InlineKeyboardButton(t(lang, "admin.wizard.back_to_server"), callback_data=f"{CB_SRV}card:{server.key}")])
    return InlineKeyboardMarkup(rows)


def _bootstrap_mode_text(server: RegisteredServer, action: str, lang: str) -> str:
    action_key = {
        "bootstrap": "admin.wizard.bootstrap",
        "reinstall": "admin.wizard.reinstall",
        "delete": "admin.wizard.delete_runtime",
    }[action]
    return "\n".join(
        [
            f"🛠 {server.flag} {server.title} ({server.key})",
            "",
            t(lang, "admin.wizard.bootstrap_mode_title", action=t(lang, action_key)),
            t(lang, "admin.wizard.bootstrap_mode_intro"),
        ]
    )


def _bootstrap_mode_markup(server_key: str, action: str, lang: str) -> InlineKeyboardMarkup:
    clean_key = "admin.wizard.clean_remove" if action == "delete" else "admin.wizard.clean_reinstall"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t(lang, "admin.wizard.keep_config"), callback_data=f"{CB_SRV}bootrun:{action}:preserve:{server_key}")],
            [InlineKeyboardButton(t(lang, clean_key), callback_data=f"{CB_SRV}bootrun:{action}:clean:{server_key}")],
            [InlineKeyboardButton(t(lang, "admin.wizard.back_to_bootstrap"), callback_data=f"{CB_SRV}bootmenu:{server_key}")],
        ]
    )


def _advanced_menu_text(server: RegisteredServer, lang: str) -> str:
    return "\n".join(
        [
            f"⚙️ {server.flag} {server.title} ({server.key})",
            "",
            t(lang, "admin.wizard.advanced_title"),
            t(lang, "admin.wizard.advanced_intro"),
        ]
    )


def _advanced_menu_markup(server_key: str, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(t(lang, "admin.wizard.advanced_general"), callback_data=f"{CB_SRV}advsection:general:{server_key}"),
                InlineKeyboardButton(t(lang, "admin.wizard.advanced_xray"), callback_data=f"{CB_SRV}advsection:xray:{server_key}"),
            ],
            [
                InlineKeyboardButton(t(lang, "admin.wizard.advanced_awg"), callback_data=f"{CB_SRV}advsection:awg:{server_key}"),
                InlineKeyboardButton(t(lang, "admin.wizard.advanced_maintenance"), callback_data=f"{CB_SRV}advsection:maintenance:{server_key}"),
            ],
            [InlineKeyboardButton(t(lang, "admin.wizard.back_to_server"), callback_data=f"{CB_SRV}card:{server_key}")],
        ]
    )


def _advanced_section_text(server: RegisteredServer, section: str, lang: str) -> str:
    if section == "general":
        return "\n".join(
            [
                f"⚙️ {server.flag} {server.title} ({server.key})",
                "",
                t(lang, "admin.wizard.advanced_general"),
                t(lang, "admin.wizard.advanced_general_intro"),
                "",
                t(lang, "admin.wizard.advanced_general_title", value=server.title),
                t(lang, "admin.wizard.advanced_general_flag", value=server.flag),
                t(lang, "admin.wizard.advanced_general_region", value=server.region),
                t(lang, "admin.wizard.advanced_general_transport", value=server.transport),
                t(lang, "admin.wizard.advanced_general_target", value=server.ssh_target or "—"),
                t(lang, "admin.wizard.advanced_general_host", value=server.public_host or "—"),
                t(lang, "admin.wizard.advanced_general_protocols", value=", ".join(server.protocol_kinds) or "—"),
                t(lang, "admin.wizard.advanced_general_ports", tcp=server.xray_tcp_port, xhttp=server.xray_xhttp_port, awg=server.awg_port),
                t(lang, "admin.wizard.advanced_general_notes", value=server.notes or "—"),
            ]
        )
    if section == "xray":
        return "\n".join(
            [
                f"⚙️ {server.flag} {server.title} ({server.key})",
                "",
                t(lang, "admin.wizard.advanced_xray"),
                t(lang, "admin.wizard.advanced_xray_intro"),
                "",
                t(lang, "admin.wizard.field_xray_tcp_port") + f": {server.xray_tcp_port}",
                t(lang, "admin.wizard.field_xray_xhttp_port") + f": {server.xray_xhttp_port}",
                t(lang, "admin.wizard.advanced_xray_host_line", value=server.xray_host or "—"),
                t(lang, "admin.wizard.advanced_xray_sni_line", value=server.xray_sni or "—"),
                t(lang, "admin.wizard.advanced_xray_fp_line", value=server.xray_fp or "—"),
            ]
        )
    if section == "awg":
        return "\n".join(
            [
                f"⚙️ {server.flag} {server.title} ({server.key})",
                "",
                t(lang, "admin.wizard.advanced_awg"),
                t(lang, "admin.wizard.advanced_awg_intro"),
                "",
                t(lang, "admin.wizard.field_awg_port") + f": {server.awg_port}",
                t(lang, "admin.wizard.advanced_awg_host_line", value=server.awg_public_host or "—"),
                t(lang, "admin.wizard.advanced_awg_iface_line", value=server.awg_iface or "—"),
                t(lang, "admin.wizard.advanced_awg_preset_line", value=server.awg_i1_preset or "quic"),
                "",
                t(lang, "admin.wizard.advanced_awg_note"),
            ]
        )
    return "\n".join(
        [
            f"⚙️ {server.flag} {server.title} ({server.key})",
            "",
            t(lang, "admin.wizard.advanced_maintenance"),
            t(lang, "admin.wizard.advanced_maintenance_intro"),
            "",
            t(lang, "admin.wizard.advanced_maintenance_note"),
        ]
    )


def _advanced_section_markup(server_key: str, section: str, lang: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]]
    if section == "general":
        rows = [
            [
                InlineKeyboardButton(t(lang, "admin.wizard.field_title"), callback_data=f"{CB_SRV}editfield:title"),
                InlineKeyboardButton(t(lang, "admin.wizard.field_flag"), callback_data=f"{CB_SRV}editfield:flag"),
            ],
            [
                InlineKeyboardButton(t(lang, "admin.wizard.field_region"), callback_data=f"{CB_SRV}editfield:region"),
                InlineKeyboardButton(t(lang, "admin.wizard.field_transport"), callback_data=f"{CB_SRV}editfield:transport"),
            ],
            [
                InlineKeyboardButton(t(lang, "admin.wizard.field_target"), callback_data=f"{CB_SRV}editfield:target"),
                InlineKeyboardButton(t(lang, "admin.wizard.field_public_host"), callback_data=f"{CB_SRV}editfield:public_host"),
            ],
            [
                InlineKeyboardButton(t(lang, "admin.wizard.field_protocols"), callback_data=f"{CB_SRV}editfield:protocols"),
                InlineKeyboardButton(t(lang, "admin.wizard.field_notes"), callback_data=f"{CB_SRV}editfield:notes"),
            ],
        ]
    elif section == "xray":
        rows = [
            [
                InlineKeyboardButton(t(lang, "admin.wizard.field_xray_host"), callback_data=f"{CB_SRV}editfield:xray_host"),
                InlineKeyboardButton(t(lang, "admin.wizard.field_xray_sni"), callback_data=f"{CB_SRV}editfield:xray_sni"),
            ],
            [
                InlineKeyboardButton(t(lang, "admin.wizard.field_xray_fp"), callback_data=f"{CB_SRV}editfield:xray_fp"),
            ],
            [
                InlineKeyboardButton(t(lang, "admin.wizard.field_xray_tcp_port"), callback_data=f"{CB_SRV}editfield:xray_tcp_port"),
                InlineKeyboardButton(t(lang, "admin.wizard.field_xray_xhttp_port"), callback_data=f"{CB_SRV}editfield:xray_xhttp_port"),
            ],
        ]
    elif section == "awg":
        rows = [
            [
                InlineKeyboardButton(t(lang, "admin.wizard.field_awg_host"), callback_data=f"{CB_SRV}editfield:awg_public_host"),
                InlineKeyboardButton(t(lang, "admin.wizard.field_awg_iface"), callback_data=f"{CB_SRV}editfield:awg_iface"),
            ],
            [
                InlineKeyboardButton(t(lang, "admin.wizard.field_awg_port"), callback_data=f"{CB_SRV}editfield:awg_port"),
                InlineKeyboardButton(t(lang, "admin.wizard.field_awg_preset"), callback_data=f"{CB_SRV}editfield:awg_i1_preset"),
            ],
            [
                InlineKeyboardButton(t(lang, "admin.wizard.awg_entropy"), callback_data=f"{CB_SRV}action:awgentropy:{server_key}"),
                InlineKeyboardButton(t(lang, "admin.wizard.awg_regen_entropy"), callback_data=f"{CB_SRV}action:awgregen:{server_key}"),
            ],
        ]
    else:
        rows = [
            [
                InlineKeyboardButton(t(lang, "admin.wizard.server_metrics"), callback_data=f"{CB_SRV}action:metrics:{server_key}"),
                InlineKeyboardButton(t(lang, "admin.wizard.check_ports"), callback_data=f"{CB_SRV}action:checkports:{server_key}"),
            ],
            [
                InlineKeyboardButton(t(lang, "admin.wizard.open_ports"), callback_data=f"{CB_SRV}action:openports:{server_key}"),
                InlineKeyboardButton(t(lang, "admin.wizard.reconcile"), callback_data=f"{CB_SRV}action:reconcile:{server_key}"),
            ],
            [
                InlineKeyboardButton(t(lang, "admin.wizard.sync_env"), callback_data=f"{CB_SRV}action:syncenv:{server_key}"),
                InlineKeyboardButton(t(lang, "admin.wizard.sync_xray"), callback_data=f"{CB_SRV}action:syncxray:{server_key}"),
            ],
            [InlineKeyboardButton(t(lang, "admin.wizard.full_cleanup"), callback_data=f"{CB_SRV}cleanupmenu:{server_key}")],
        ]
    rows.append([InlineKeyboardButton(t(lang, "admin.wizard.back_to_advanced"), callback_data=f"{CB_SRV}advanced:{server_key}")])
    return InlineKeyboardMarkup(rows)


def _render_server_card(context: CallbackContext, server_key: str) -> None:
    server = get_server(server_key)
    if not server:
        _wizard_edit(context, t(_wizard_lang(context), "admin.wizard.server_not_found"), kb_back_menu(_wizard_lang(context)))
        return
    _wizard_edit(context, _server_card_text(server, _wizard_lang(context)), _server_card_markup(server.key, _wizard_lang(context)))


def _open_advanced_menu(context: CallbackContext, server_key: str) -> None:
    server = get_server(server_key)
    if not server:
        _wizard_edit(context, t(_wizard_lang(context), "admin.wizard.server_not_found"), kb_back_menu(_wizard_lang(context)))
        return
    w = _wizard_get(context)
    if w is not None:
        w["server_key"] = server_key
        w["data"] = _load_server_into_data(server)
        w["step"] = "advanced"
        w["advanced_section"] = None
        _wizard_set(context, w)
    _wizard_edit(context, _advanced_menu_text(server, _wizard_lang(context)), _advanced_menu_markup(server_key, _wizard_lang(context)))


def _open_bootstrap_menu(context: CallbackContext, server_key: str) -> None:
    server = get_server(server_key)
    if not server:
        _wizard_edit(context, t(_wizard_lang(context), "admin.wizard.server_not_found"), kb_back_menu(_wizard_lang(context)))
        return
    w = _wizard_get(context)
    if w is not None:
        w["server_key"] = server_key
        w["step"] = "bootstrap_menu"
        _wizard_set(context, w)
    _wizard_edit(context, _bootstrap_menu_text(server, _wizard_lang(context)), _bootstrap_menu_markup(server, _wizard_lang(context)))


def _open_bootstrap_mode(context: CallbackContext, server_key: str, action: str) -> None:
    server = get_server(server_key)
    if not server:
        _wizard_edit(context, t(_wizard_lang(context), "admin.wizard.server_not_found"), kb_back_menu(_wizard_lang(context)))
        return
    w = _wizard_get(context)
    if w is not None:
        w["server_key"] = server_key
        w["step"] = f"bootstrap_mode_{action}"
        _wizard_set(context, w)
    _wizard_edit(context, _bootstrap_mode_text(server, action, _wizard_lang(context)), _bootstrap_mode_markup(server_key, action, _wizard_lang(context)))


def _full_cleanup_text(server: RegisteredServer, lang: str) -> str:
    lines = [
        f"🧨 {server.flag} {server.title} ({server.key})",
        "",
        t(lang, "admin.wizard.full_cleanup_title"),
        t(lang, "admin.wizard.full_cleanup_intro"),
        "",
        t(lang, "admin.wizard.full_cleanup_scope"),
    ]
    if server.transport == "ssh":
        lines.extend(["", t(lang, "admin.wizard.full_cleanup_scope_ssh")])
    return "\n".join(lines)


def _full_cleanup_markup(server: RegisteredServer, lang: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(t(lang, "admin.wizard.full_cleanup_runtime_only"), callback_data=f"{CB_SRV}cleanuprun:runtime:{server.key}")]
    ]
    if server.transport == "ssh":
        rows.append(
            [InlineKeyboardButton(t(lang, "admin.wizard.full_cleanup_with_ssh"), callback_data=f"{CB_SRV}cleanuprun:runtime_ssh:{server.key}")]
        )
    rows.append([InlineKeyboardButton(t(lang, "admin.wizard.back_to_maintenance"), callback_data=f"{CB_SRV}advsection:maintenance:{server.key}")])
    return InlineKeyboardMarkup(rows)


def _open_full_cleanup_menu(context: CallbackContext, server_key: str) -> None:
    server = get_server(server_key)
    if not server:
        _wizard_edit(context, t(_wizard_lang(context), "admin.wizard.server_not_found"), kb_back_menu(_wizard_lang(context)))
        return
    w = _wizard_get(context)
    if w is not None:
        w["server_key"] = server_key
        w["step"] = "full_cleanup"
        _wizard_set(context, w)
    _wizard_edit(context, _full_cleanup_text(server, _wizard_lang(context)), _full_cleanup_markup(server, _wizard_lang(context)))


def _open_advanced_section(context: CallbackContext, server_key: str, section: str) -> None:
    server = get_server(server_key)
    if not server:
        _wizard_edit(context, t(_wizard_lang(context), "admin.wizard.server_not_found"), kb_back_menu(_wizard_lang(context)))
        return
    w = _wizard_get(context)
    if w is not None:
        w["server_key"] = server_key
        w["data"] = _load_server_into_data(server)
        w["step"] = f"advanced_{section}"
        w["advanced_section"] = section
        _wizard_set(context, w)
    _wizard_edit(context, _advanced_section_text(server, section, _wizard_lang(context)), _advanced_section_markup(server_key, section, _wizard_lang(context)))


def _localize_action_output(out: str, lang: str, *, server_key: str | None = None) -> str:
    body = (out or "").strip()
    if not body:
        return t(lang, "admin.wizard.no_output")

    if body.startswith("DOCKER_INSTALL_STATUS|"):
        parts = body.splitlines()
        header = parts[0].split("|")
        details = "\n".join(parts[1:]).strip()
        status = header[1] if len(header) > 1 else "error"
        mode = header[2] if len(header) > 2 else "unknown"
        if status == "ok":
            if mode == "available_via_sudo":
                return t(lang, "admin.wizard.docker_install_ok_sudo")
            return t(lang, "admin.wizard.docker_install_ok")
        msg = t(lang, "admin.wizard.docker_install_error")
        if details:
            msg += "\n\n" + details[-1500:]
        return msg

    missing_block_ru = (
        "Docker недоступен на сервере.\n\n"
        "Bootstrap не может продолжиться без рабочего Docker.\n"
        "Установи и запусти Docker, затем повтори Probe или Bootstrap.\n\n"
        "Рекомендуемые команды:\n"
        "apt-get update\n"
        "apt-get install -y docker.io\n"
        "apt-cache show docker-compose-plugin >/dev/null 2>&1 && apt-get install -y docker-compose-plugin || true\n"
        "systemctl enable --now docker || service docker start"
    )
    sudo_block_ru = (
        "Docker доступен только через sudo.\n\n"
        "Для bootstrap это обычно нормально, но если на сервере дальше возникают ошибки доступа к Docker, проверь права пользователя или группу docker."
    )
    if missing_block_ru in body:
        body = body.replace(missing_block_ru, t(lang, "admin.wizard.docker_missing_block"))
    if sudo_block_ru in body:
        body = body.replace(sudo_block_ru, t(lang, "admin.wizard.docker_sudo_block"))
    probe_summary = _format_probe_output(body, lang, server_key=server_key)
    if probe_summary:
        return probe_summary
    if lang == "en":
        replacements = [
            ("Сводка по портам:", "Port summary:"),
            ("используется управляемым рантаймом", "managed runtime"),
            ("свободен", "free"),
            ("занят", "busy"),
            ("открыт в firewall", "firewall open"),
            ("закрыт в firewall", "firewall closed"),
            ("рекомендуемый порт", "suggested port"),
            ("Проверка портов пропущена", "Port check skipped"),
            ("Файл node.env записан в /etc/node-plane/node.env", "node.env has been written to /etc/node-plane/node.env"),
            ("Правила firewall обновлены.", "Firewall rules updated."),
            ("Открыты правила firewall:", "Opened firewall rules:"),
            ("Для этого сервера нет управляемых портов", "There are no managed ports for this server"),
            ("пользователь:", "user:"),
            ("ядро:", "kernel:"),
            ("docker: доступен через sudo", "docker: available via sudo"),
            ("docker: доступен", "docker: available"),
            ("docker: недоступен", "docker: unavailable"),
            ("tun: доступен", "tun: available"),
            ("tun: отсутствует", "tun: missing"),
            ("awg_userspace_ready: да", "awg_userspace_ready: yes"),
            ("awg_userspace_ready: нет", "awg_userspace_ready: no"),
            ("Bootstrap завершён.", "Bootstrap completed."),
            ("Рабочий node.env записан в /etc/node-plane/node.env.", "Working node.env has been written to /etc/node-plane/node.env."),
            ("Рантайм удалён.", "Runtime removed."),
            ("Существующие конфиги сохранены.", "Existing configs preserved."),
            ("Конфиги и директории рантайма удалены.", "Runtime configs and directories removed."),
            ("Установлены базовые пакеты и служебные скрипты", "Base packages and helper scripts installed"),
            ("Конфиг Xray сохранён, рантайм развёрнут заново", "Xray config preserved, runtime redeployed"),
            ("Настройки Xray сгенерированы, рантайм развёрнут", "Xray settings generated, runtime deployed"),
            ("Конфиг AWG сохранён, рантайм развёрнут заново", "AWG config preserved, runtime redeployed"),
            ("Рантайм AWG развёрнут", "AWG runtime deployed"),
            ("Режим переустановки: с сохранением существующего конфига.", "Reinstall mode: keep existing config."),
            ("Режим переустановки: чистая переустановка.", "Reinstall mode: clean reinstall."),
            ("Рантайм удалён.", "Runtime removed."),
            ("Существующие конфиги сохранены.", "Existing configs preserved."),
            ("Конфиги и директории рантайма удалены.", "Runtime configs and directories removed."),
        ]
        for src, dst in replacements:
            body = body.replace(src, dst)
    return body


def _localize_probe_port_line(line: str, lang: str) -> str:
    if lang != "en":
        return line
    replacements = [
        ("Сводка по портам:", "Port summary:"),
        ("используется управляемым рантаймом", "managed runtime"),
        ("свободен", "free"),
        ("занят", "busy"),
        ("открыт в firewall", "firewall open"),
        ("закрыт в firewall", "firewall closed"),
        ("рекомендуемый порт", "suggested port"),
    ]
    for src, dst in replacements:
        line = line.replace(src, dst)
    return line


def _format_probe_output(body: str, lang: str, *, server_key: str | None = None) -> str | None:
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not any(line.startswith("hostname:") for line in lines):
        return None

    hostname = next((line.split(":", 1)[1].strip() for line in lines if line.startswith("hostname:")), "—")
    user = next((line.split(":", 1)[1].strip() for line in lines if line.startswith("пользователь:") or line.startswith("user:")), "—")
    kernel = next((line.split(":", 1)[1].strip() for line in lines if line.startswith("ядро:") or line.startswith("kernel:")), "—")
    docker_line = next((line for line in lines if line.startswith("docker:")), "")
    tun_line = next((line for line in lines if line.startswith("tun:")), "")
    awg_line = next((line for line in lines if line.startswith("awg_userspace_ready:")), "")
    port_lines = [
        line
        for line in lines
        if line.startswith("- ")
        and (
            "xray " in line.lower()
            or "awg " in line.lower()
            or "tcp:" in line.lower()
            or "xhttp:" in line.lower()
        )
    ]

    ready: list[str] = []
    bot_fix: list[str] = []
    manual_fix: list[str] = []
    unsupported: list[str] = []

    for line in lines:
        if not line.startswith("PROBE_UNSUPPORTED|"):
            continue
        tag = line.split("|", 1)[1].strip()
        if tag == "local_in_container":
            unsupported.append(t(lang, "admin.wizard.probe_unsupported_local_container"))

    if docker_line == "docker: доступен" or docker_line == "docker: available":
        ready.append(t(lang, "admin.wizard.probe_ready_docker"))
    elif docker_line == "docker: доступен через sudo" or docker_line == "docker: available via sudo":
        ready.append(t(lang, "admin.wizard.probe_ready_docker_sudo"))
    else:
        bot_fix.append(t(lang, "admin.wizard.probe_fix_docker"))

    if tun_line.endswith("доступен") or tun_line.endswith("available"):
        ready.append(t(lang, "admin.wizard.probe_ready_tun"))
    else:
        manual_fix.append(t(lang, "admin.wizard.probe_fix_tun"))

    if awg_line.endswith("да") or awg_line.endswith("yes"):
        ready.append(t(lang, "admin.wizard.probe_ready_awg"))
    else:
        if "docker:" in docker_line and ("недоступен" in docker_line or "unavailable" in docker_line):
            bot_fix.append(t(lang, "admin.wizard.probe_fix_awg_after_docker"))
        else:
            manual_fix.append(t(lang, "admin.wizard.probe_fix_awg"))

    for line in port_lines:
        normalized = line.lower()
        localized_line = _localize_probe_port_line(line, lang)
        if ("свободен" in normalized or "managed runtime" in normalized or "используется управляемым рантаймом" in normalized) and (
            "открыт в firewall" in normalized or "firewall open" in normalized
        ):
            ready.append(localized_line)
            continue
        if "закрыт в firewall" in normalized or "firewall closed" in normalized:
            bot_fix.append(localized_line)
        elif "занят" in normalized or "busy" in normalized:
            manual_fix.append(localized_line)
        else:
            ready.append(localized_line)

    sections = [
        t(lang, "admin.wizard.probe_summary_title"),
        "",
        t(lang, "admin.wizard.probe_host_line", hostname=hostname),
        t(lang, "admin.wizard.probe_user_line", user=user),
        t(lang, "admin.wizard.probe_kernel_line", kernel=kernel),
    ]

    if ready:
        sections.extend(["", t(lang, "admin.wizard.probe_section_ready")])
        sections.extend([f"• {item}" for item in ready])
    if bot_fix:
        sections.extend(["", t(lang, "admin.wizard.probe_section_bot_fix")])
        sections.extend([f"• {item}" for item in bot_fix])
    if manual_fix:
        sections.extend(["", t(lang, "admin.wizard.probe_section_manual_fix")])
        sections.extend([f"• {item}" for item in manual_fix])
    if unsupported:
        sections.extend(["", t(lang, "admin.wizard.probe_section_unsupported")])
        sections.extend([f"• {item}" for item in unsupported])

    next_steps: list[str] = []
    server = get_server(server_key) if server_key else None
    if any(item == t(lang, "admin.wizard.probe_fix_docker") for item in bot_fix):
        next_steps.append(t(lang, "admin.wizard.probe_next_install_docker"))
    if any("firewall" in item.lower() or "ufw allow" in item.lower() for item in bot_fix):
        next_steps.append(t(lang, "admin.wizard.probe_next_open_ports"))
    if unsupported:
        next_steps.append(t(lang, "admin.wizard.probe_next_use_supported_mode"))
    elif not bot_fix and not manual_fix:
        if server and server.bootstrap_state == "bootstrapped":
            next_steps.append(t(lang, "admin.wizard.probe_next_return_to_server"))
        else:
            next_steps.append(t(lang, "admin.wizard.probe_next_bootstrap"))
    elif not manual_fix:
        next_steps.append(t(lang, "admin.wizard.probe_next_rerun"))
    if next_steps:
        sections.extend(["", t(lang, "admin.wizard.probe_section_next")])
        sections.extend([f"• {item}" for item in next_steps])

    return "\n".join(sections)


def _action_result_text(title: str, rc: int, out: str, back_key: str, lang: str) -> str:
    body = _localize_action_output(out, lang, server_key=back_key)
    if title == t(lang, "admin.wizard.probe"):
        has_probe_issues = any(
            section in body
            for section in (
                t(lang, "admin.wizard.probe_section_bot_fix"),
                t(lang, "admin.wizard.probe_section_manual_fix"),
                t(lang, "admin.wizard.probe_section_unsupported"),
            )
        )
        status = "⚠️" if has_probe_issues else "✅"
    else:
        status = "✅" if rc == 0 else "⚠️"
    if len(body) > 2500:
        body = body[-2500:]
    return f"{status} {title}\n\n{body}\n\n{t(lang, 'admin.wizard.server_label')}: {back_key}"


def _summary_text(data: Dict[str, Any], editing: bool = False, lang: str = "ru") -> str:
    protocols = ", ".join(sorted(data["protocol_kinds"])) or "—"
    target = data["target"] or "—"
    public_host = data["public_host"] or "—"
    action = ("Изменение" if editing else "Базовая настройка") if lang == "ru" else ("Editing" if editing else "Basic setup")
    next_step = (
        "После сохранения можно открыть Advanced и настроить Xray, AWG и maintenance."
        if lang == "ru"
        else "After saving you can open Advanced to adjust Xray, AWG, and maintenance settings."
    )
    return (
        f"🖥 {action} {'сервера' if lang == 'ru' else 'server'}\n\n"
        f"• key: {_md(data['key'])}\n"
        f"• title: {_md(data['title'])}\n"
        f"• flag: {_md(data['flag'])}\n"
        f"• region: {_md(data['region'])}\n"
        f"• transport: {_md(data['transport'])}\n"
        f"• target: {_md(target)}\n"
        f"• public_host: {_md(public_host)}\n"
        f"• protocols: {_md(protocols)}\n\n"
        f"{next_step}"
    )


def _summary_markup(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💾 Сохранить" if lang == "ru" else "💾 Save", callback_data=f"{CB_SRV}save")],
            [InlineKeyboardButton(t(lang, "menu.back"), callback_data=f"{CB_SRV}back")],
        ]
    )


def _persist_edited_server(w: Dict[str, Any], lang: str) -> tuple[Optional[RegisteredServer], Optional[str]]:
    data = w["data"]
    current = get_server(data["key"])
    if not current:
        return None, t(lang, "admin.wizard.server_not_found")

    target_value = "" if data.get("transport") == "local" else str(data.get("target") or "").strip()
    public_host_value = str(data.get("public_host") or "").strip()
    try:
        sanitized = {
            "title": validate_server_field("title", data["title"]),
            "flag": validate_server_field("flag", data["flag"]),
            "region": validate_server_field("region", data["region"]),
            "transport": validate_server_field("transport", data["transport"]),
            "public_host": validate_server_field("public_host", public_host_value or (target_value.split("@")[-1] if target_value else "")),
            "ssh_host": validate_server_field("ssh_host", target_value or ""),
            "protocol_kinds": validate_server_field("protocol_kinds", sorted(data["protocol_kinds"])),
            "xray_host": validate_server_field("xray_host", data.get("xray_host") or ""),
            "xray_sni": validate_server_field("xray_sni", data.get("xray_sni") or ""),
            "xray_fp": validate_server_field("xray_fp", data.get("xray_fp") or "chrome"),
            "xray_tcp_port": validate_server_field("xray_tcp_port", int(data.get("xray_tcp_port") or 443)),
            "xray_xhttp_port": validate_server_field("xray_xhttp_port", int(data.get("xray_xhttp_port") or 8443)),
            "notes": validate_server_field("notes", data.get("notes") or ""),
            "awg_public_host": validate_server_field("awg_public_host", data.get("awg_public_host") or ""),
            "awg_port": validate_server_field("awg_port", int(data.get("awg_port") or 51820)),
            "awg_iface": validate_server_field("awg_iface", data.get("awg_iface") or "wg0"),
            "awg_i1_preset": validate_server_field("awg_i1_preset", data.get("awg_i1_preset") or "quic"),
        }
    except ValueError as exc:
        return None, str(exc)

    runtime_edited = any(
        [
            current.transport != sanitized["transport"],
            (current.public_host or "") != sanitized["public_host"],
            (current.ssh_host or "") != sanitized["ssh_host"],
            tuple(current.protocol_kinds) != tuple(sanitized["protocol_kinds"]),
            current.xray_host != sanitized["xray_host"],
            current.xray_sni != sanitized["xray_sni"],
            current.xray_fp != sanitized["xray_fp"],
            current.xray_tcp_port != sanitized["xray_tcp_port"],
            current.xray_xhttp_port != sanitized["xray_xhttp_port"],
            current.awg_public_host != sanitized["awg_public_host"],
            current.awg_port != sanitized["awg_port"],
            current.awg_iface != sanitized["awg_iface"],
        ]
    )
    server = update_server_fields(
        data["key"],
        title=sanitized["title"],
        flag=sanitized["flag"],
        region=sanitized["region"],
        transport=sanitized["transport"],
        public_host=sanitized["public_host"],
        ssh_host=sanitized["ssh_host"] or None,
        protocol_kinds=sanitized["protocol_kinds"],
        xray_host=sanitized["xray_host"],
        xray_sni=sanitized["xray_sni"],
        xray_fp=sanitized["xray_fp"],
        xray_tcp_port=sanitized["xray_tcp_port"],
        xray_xhttp_port=sanitized["xray_xhttp_port"],
        notes=sanitized["notes"],
        awg_public_host=sanitized["awg_public_host"],
        awg_port=sanitized["awg_port"],
        awg_iface=sanitized["awg_iface"],
        awg_i1_preset=sanitized["awg_i1_preset"],
        bootstrap_state="edited" if runtime_edited else current.bootstrap_state,
    )
    w["data"] = _load_server_into_data(server)
    w["edit_single"] = False
    w["step"] = "advanced"
    return server, None


def _keep_current(text: str, current: str) -> str:
    if text == ".":
        return current
    return text or current


def _render_step_prompt(context: CallbackContext, lang: str, step: str, data: Dict[str, Any]) -> None:
    prompts = {
        "key": t(lang, "admin.wizard.server_create_key"),
        "title": t(lang, "admin.wizard.server_create_title"),
        "flag": t(lang, "admin.wizard.server_create_flag", flag=data["flag"]),
        "region": t(lang, "admin.wizard.server_create_region"),
        "target": t(lang, "admin.wizard.server_enter_target"),
        "public_host": t(lang, "admin.wizard.server_enter_public_host_local")
        if data.get("transport") == "local"
        else t(lang, "admin.wizard.server_enter_public_host"),
        "notes": "Введи заметки для сервера или `.` чтобы оставить как есть." if lang == "ru" else "Enter server notes or `.` to keep the current value.",
    }
    next_payload = f"{CB_SRV}next"
    if step in {"notes", "xray_sni", "xray_fp"}:
        next_payload = None
    _wizard_edit(context, prompts[step], _step_nav_markup(lang, show_back=True, next_payload=next_payload))


def _start_create_flow(
    context: CallbackContext,
    w: Dict[str, Any],
    lang: str,
    *,
    transport: str | None = None,
) -> None:
    w["mode"] = "create"
    w["step"] = "key"
    w["edit_single"] = False
    w["transport_locked"] = bool(transport)
    w["data"] = {
        "key": "",
        "title": "",
        "flag": "🏳️",
        "region": "",
        "transport": transport or "ssh",
        "target": "",
        "public_host": "",
        "notes": "",
        "protocol_kinds": set(),
        "awg_i1_preset": "quic",
    }
    _wizard_set(context, w)
    _render_step_prompt(context, lang, "key", w["data"])


def serverwizard_cmd(update: Update, context: CallbackContext) -> None:
    if not guard(update):
        return
    lang = get_locale_for_update(update)
    sent = update.effective_message.reply_text(t(lang, "admin.wizard.server_menu"))
    w = _wizard_init(sent, "menu")
    w["locale"] = lang
    _wizard_set(context, w)
    servers = list_servers(include_disabled=True)
    _wizard_edit(context, _server_dashboard_text(servers, lang), _server_dashboard_markup(servers, lang))
    safe_delete_update_message(update, context)


def server_wizard_text(update: Update, context: CallbackContext) -> None:
    w = _wizard_get(context)
    if not w or not w.get("active"):
        return
    text = (update.effective_message.text or "").strip()
    data = w["data"]
    step = w["step"]
    lang = _wizard_lang(context)

    if step == "key":
        value = text.lower().strip()
        if not value:
            _wizard_edit(context, t(lang, "admin.wizard.server_create_key"), InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "admin.wizard.cancel"), callback_data=f"{CB_SRV}cancel")]]))
            return
        try:
            data["key"] = validate_server_key(value)
        except ValueError as exc:
            _wizard_edit(context, str(exc), InlineKeyboardMarkup([[InlineKeyboardButton(t(lang, "admin.wizard.cancel"), callback_data=f"{CB_SRV}cancel")]]))
            return
        w["step"] = "title"
        _wizard_set(context, w)
        _render_step_prompt(context, lang, "title", data)
        safe_delete_update_message(update, context)
        return

    if step == "title":
        data["title"] = _keep_current(text, data["title"])
        if w["mode"] == "edit" and w.get("edit_single"):
            server, err = _persist_edited_server(w, lang)
            if err or not server:
                _wizard_edit(context, err or t(lang, "admin.wizard.server_not_found"), kb_back_menu(lang))
                return
            server_key = server.key
            section = str(w.get("advanced_section") or "general")
            _wizard_set(context, w)
            _open_advanced_section(context, server_key, section)
            safe_delete_update_message(update, context)
            return
        w["step"] = "flag"
        _wizard_set(context, w)
        _render_step_prompt(context, lang, "flag", data)
        safe_delete_update_message(update, context)
        return

    if step == "flag":
        data["flag"] = _keep_current(text, data["flag"])
        if w["mode"] == "edit" and w.get("edit_single"):
            server, err = _persist_edited_server(w, lang)
            if err or not server:
                _wizard_edit(context, err or t(lang, "admin.wizard.server_not_found"), kb_back_menu(lang))
                return
            server_key = server.key
            section = str(w.get("advanced_section") or "general")
            _wizard_set(context, w)
            _open_advanced_section(context, server_key, section)
            safe_delete_update_message(update, context)
            return
        w["step"] = "region"
        _wizard_set(context, w)
        _render_step_prompt(context, lang, "region", data)
        safe_delete_update_message(update, context)
        return

    if step == "region":
        data["region"] = _keep_current(text, data["region"])
        if w["mode"] == "edit" and w.get("edit_single"):
            server, err = _persist_edited_server(w, lang)
            if err or not server:
                _wizard_edit(context, err or t(lang, "admin.wizard.server_not_found"), kb_back_menu(lang))
                return
            server_key = server.key
            section = str(w.get("advanced_section") or "general")
            _wizard_set(context, w)
            _open_advanced_section(context, server_key, section)
            safe_delete_update_message(update, context)
            return
        if w.get("transport_locked"):
            if data.get("transport") == "local":
                w["step"] = "public_host"
                _wizard_set(context, w)
                _render_step_prompt(context, lang, "public_host", data)
            else:
                w["step"] = "target"
                _wizard_set(context, w)
                _render_step_prompt(context, lang, "target", data)
        else:
            w["step"] = "transport"
            _wizard_set(context, w)
            _wizard_edit(context, t(lang, "admin.wizard.server_choose_transport"), _transport_markup(lang))
        safe_delete_update_message(update, context)
        return

    if step == "target":
        data["target"] = _keep_current(text, data["target"])
        if w["mode"] == "edit" and w.get("edit_single"):
            server, err = _persist_edited_server(w, lang)
            if err or not server:
                _wizard_edit(context, err or t(lang, "admin.wizard.server_not_found"), kb_back_menu(lang))
                return
            server_key = server.key
            section = str(w.get("advanced_section") or "general")
            _wizard_set(context, w)
            _open_advanced_section(context, server_key, section)
            safe_delete_update_message(update, context)
            return
        w["step"] = "public_host"
        _wizard_set(context, w)
        _render_step_prompt(context, lang, "public_host", data)
        safe_delete_update_message(update, context)
        return

    if step == "public_host":
        data["public_host"] = _keep_current(text, data["public_host"])
        if w["mode"] == "edit" and w.get("edit_single"):
            server, err = _persist_edited_server(w, lang)
            if err or not server:
                _wizard_edit(context, err or t(lang, "admin.wizard.server_not_found"), kb_back_menu(lang))
                return
            server_key = server.key
            section = str(w.get("advanced_section") or "general")
            _wizard_set(context, w)
            _open_advanced_section(context, server_key, section)
            safe_delete_update_message(update, context)
            return
        w["step"] = "protocols"
        _wizard_set(context, w)
        _wizard_edit(context, t(lang, "admin.wizard.server_choose_protocols"), _protocol_markup(data["protocol_kinds"], lang))
        safe_delete_update_message(update, context)
        return

    if step == "notes":
        data["notes"] = _keep_current(text, data.get("notes", ""))
        if w["mode"] == "edit":
            server, err = _persist_edited_server(w, lang)
            if err or not server:
                _wizard_edit(context, err or t(lang, "admin.wizard.server_not_found"), kb_back_menu(lang))
                return
            server_key = server.key
            section = str(w.get("advanced_section") or "general")
            _wizard_set(context, w)
            _open_advanced_section(context, server_key, section)
        safe_delete_update_message(update, context)
        return

    if step in {"xray_host", "xray_sni", "xray_fp", "awg_public_host", "awg_iface"}:
        data[step] = _keep_current(text, data.get(step, ""))
        server_key = str(w.get("server_key") or data["key"])
        section = str(w.get("advanced_section") or _advanced_section_for_field(step))
        if w["mode"] == "edit":
            server, err = _persist_edited_server(w, lang)
            if err or not server:
                _wizard_edit(context, err or t(lang, "admin.wizard.server_not_found"), kb_back_menu(lang))
                return
            server_key = server.key
        w["step"] = f"advanced_{section}"
        _wizard_set(context, w)
        _open_advanced_section(context, server_key, section)
        safe_delete_update_message(update, context)
        return

    if step in {"xray_tcp_port", "xray_xhttp_port", "awg_port"}:
        if text != ".":
            data[step] = int(text)
        server_key = str(w.get("server_key") or data["key"])
        section = str(w.get("advanced_section") or "general")
        if w["mode"] == "edit":
            server, err = _persist_edited_server(w, lang)
            if err or not server:
                _wizard_edit(context, err or t(lang, "admin.wizard.server_not_found"), kb_back_menu(lang))
                return
            server_key = server.key
        w["step"] = f"advanced_{section}"
        _wizard_set(context, w)
        _open_advanced_section(context, server_key, section)
        safe_delete_update_message(update, context)
        return


def _load_server_into_data(server: RegisteredServer) -> Dict[str, Any]:
    return {
        "key": server.key,
        "title": server.title,
        "flag": server.flag,
        "region": server.region,
        "transport": server.transport,
        "target": server.ssh_target or "",
        "public_host": server.public_host or "",
        "notes": server.notes or "",
        "protocol_kinds": set(server.protocol_kinds),
        "xray_host": server.xray_host or "",
        "xray_sni": server.xray_sni or "",
        "xray_fp": server.xray_fp or "chrome",
        "xray_tcp_port": server.xray_tcp_port,
        "xray_xhttp_port": server.xray_xhttp_port,
        "awg_public_host": server.awg_public_host or "",
        "awg_port": server.awg_port,
        "awg_iface": server.awg_iface or "wg0",
        "awg_i1_preset": server.awg_i1_preset or "quic",
    }


def on_server_callback(update: Update, context: CallbackContext, payload: str) -> None:
    answer_cb(update)
    if not guard(update):
        return
    lang = get_locale_for_update(update)

    if payload == "menu":
        sent = update.callback_query.message
        w = _wizard_init(sent, "menu")
        w["locale"] = lang
        _wizard_set(context, w)
        servers = list_servers(include_disabled=True)
        _wizard_edit(context, _server_dashboard_text(servers, lang), _server_dashboard_markup(servers, lang))
        return

    w = _wizard_get(context)
    if payload == "cancel":
        if not w:
            sent = update.callback_query.message
            w = _wizard_init(sent, "menu")
            w["locale"] = lang
        servers = list_servers(include_disabled=True)
        w["step"] = "menu"
        _wizard_set(context, w)
        _wizard_edit(context, _server_dashboard_text(servers, lang), _server_dashboard_markup(servers, lang))
        return
    if payload in {"start:create", "start:create_local", "start:create_remote"} and not w:
        sent = update.callback_query.message
        w = _wizard_init(sent, "menu")
        w["locale"] = lang
        _wizard_set(context, w)
    if payload.startswith("card:") and not w:
        sent = update.callback_query.message
        w = _wizard_init(sent, "menu")
        w["locale"] = lang
        _wizard_set(context, w)
    if not w:
        safe_edit_message(update, context, t(lang, "admin.wizard.server_inactive"), reply_markup=kb_back_menu(lang), parse_mode=None)
        return

    data = w["data"]
    lang = _wizard_lang(context)

    if payload == "list":
        if not w:
            sent = update.callback_query.message
            w = _wizard_init(sent, "menu")
            w["locale"] = lang
        servers = list_servers(include_disabled=True)
        w["step"] = "menu"
        _wizard_set(context, w)
        _wizard_edit(context, _server_dashboard_text(servers, lang), _server_dashboard_markup(servers, lang))
        return

    if payload == "next":
        step = str(w.get("step") or "")
        if step == "key":
            if not str(data.get("key") or "").strip():
                _render_step_prompt(context, lang, "key", data)
                return
            w["step"] = "title"
            _wizard_set(context, w)
            _render_step_prompt(context, lang, "title", data)
            return
        if step == "title":
            if not str(data.get("title") or "").strip():
                _render_step_prompt(context, lang, "title", data)
                return
            w["step"] = "flag"
            _wizard_set(context, w)
            _render_step_prompt(context, lang, "flag", data)
            return
        if step == "flag":
            w["step"] = "region"
            _wizard_set(context, w)
            _render_step_prompt(context, lang, "region", data)
            return
        if step == "region":
            if not str(data.get("region") or "").strip():
                _render_step_prompt(context, lang, "region", data)
                return
            if w.get("transport_locked"):
                if data.get("transport") == "local":
                    w["step"] = "public_host"
                    _wizard_set(context, w)
                    _render_step_prompt(context, lang, "public_host", data)
                else:
                    w["step"] = "target"
                    _wizard_set(context, w)
                    _render_step_prompt(context, lang, "target", data)
            else:
                w["step"] = "transport"
                _wizard_set(context, w)
                _wizard_edit(context, t(lang, "admin.wizard.server_choose_transport"), _transport_markup(lang))
            return
        if step == "transport":
            if data.get("transport") == "local":
                data["target"] = ""
                w["step"] = "public_host"
                _wizard_set(context, w)
                _render_step_prompt(context, lang, "public_host", data)
                return
            w["step"] = "target"
            _wizard_set(context, w)
            _render_step_prompt(context, lang, "target", data)
            return
        if step == "target":
            if data.get("transport") == "ssh" and not str(data.get("target") or "").strip():
                _render_step_prompt(context, lang, "target", data)
                return
            w["step"] = "public_host"
            _wizard_set(context, w)
            _render_step_prompt(context, lang, "public_host", data)
            return
        if step == "public_host":
            w["step"] = "protocols"
            _wizard_set(context, w)
            _wizard_edit(context, t(lang, "admin.wizard.server_choose_protocols"), _protocol_markup(data["protocol_kinds"], lang))
            return
        if step == "notes":
            server_key = str(w.get("server_key") or data["key"])
            section = str(w.get("advanced_section") or "general")
            _wizard_set(context, w)
            _open_advanced_section(context, server_key, section)
            return

    if payload.startswith("card:"):
        _render_server_card(context, payload.split(":", 1)[1])
        return

    if payload.startswith("advanced:"):
        _open_advanced_menu(context, payload.split(":", 1)[1])
        return

    if payload.startswith("advsection:"):
        _, section, server_key = payload.split(":", 2)
        _open_advanced_section(context, server_key, section)
        return

    if payload == "back":
        if w["mode"] == "create":
            if w["step"] == "key":
                servers = list_servers(include_disabled=True)
                w["step"] = "menu"
                _wizard_set(context, w)
                _wizard_edit(context, _server_dashboard_text(servers, lang), _server_dashboard_markup(servers, lang))
                return
            if w["step"] == "title":
                w["step"] = "key"
                _wizard_set(context, w)
                _render_step_prompt(context, lang, "key", data)
                return
            if w["step"] == "flag":
                w["step"] = "title"
                _wizard_set(context, w)
                _render_step_prompt(context, lang, "title", data)
                return
            if w["step"] == "region":
                w["step"] = "flag"
                _wizard_set(context, w)
                _render_step_prompt(context, lang, "flag", data)
                return
            if w["step"] == "transport":
                w["step"] = "region"
                _wizard_set(context, w)
                _render_step_prompt(context, lang, "region", data)
                return
            if w["step"] == "target":
                if w.get("transport_locked"):
                    w["step"] = "region"
                    _wizard_set(context, w)
                    _render_step_prompt(context, lang, "region", data)
                else:
                    w["step"] = "transport"
                    _wizard_set(context, w)
                    _wizard_edit(context, t(lang, "admin.wizard.server_choose_transport"), _transport_markup(lang))
                return
            if w["step"] == "public_host":
                if w.get("transport_locked"):
                    w["step"] = "region"
                    _wizard_set(context, w)
                    _render_step_prompt(context, lang, "region", data)
                elif data["transport"] == "local":
                    w["step"] = "transport"
                    _wizard_set(context, w)
                    _wizard_edit(context, t(lang, "admin.wizard.server_choose_transport"), _transport_markup(lang))
                else:
                    w["step"] = "target"
                    _wizard_set(context, w)
                    _render_step_prompt(context, lang, "target", data)
                return
            if w["step"] == "protocols":
                w["step"] = "public_host"
                _wizard_set(context, w)
                _render_step_prompt(context, lang, "public_host", data)
                return
        else:
            if w["step"] == "pick":
                servers = list_servers(include_disabled=True)
                w["step"] = "menu"
                _wizard_set(context, w)
                _wizard_edit(context, _server_dashboard_text(servers, lang), _server_dashboard_markup(servers, lang))
                return
            if w["step"] == "title":
                if w.get("edit_single"):
                    _open_advanced_section(context, str(w.get("server_key") or data["key"]), str(w.get("advanced_section") or "general"))
                elif w.get("server_key"):
                    _render_server_card(context, str(w["server_key"]))
                else:
                    servers = list_servers(include_disabled=True)
                    _wizard_edit(context, _server_dashboard_text(servers, lang), _server_dashboard_markup(servers, lang))
                return
            if w["step"] == "flag":
                if w.get("edit_single"):
                    _open_advanced_section(context, str(w.get("server_key") or data["key"]), str(w.get("advanced_section") or "general"))
                else:
                    w["step"] = "title"
                    _wizard_set(context, w)
                    _wizard_edit(context, t(lang, "admin.wizard.server_edit", server=data["key"], title=data["title"]), _step_nav_markup(lang, next_payload=f"{CB_SRV}next"))
                return
            if w["step"] == "region":
                if w.get("edit_single"):
                    _open_advanced_section(context, str(w.get("server_key") or data["key"]), str(w.get("advanced_section") or "general"))
                else:
                    w["step"] = "flag"
                    _wizard_set(context, w)
                    _render_step_prompt(context, lang, "flag", data)
                return
            if w["step"] == "transport":
                if w.get("edit_single"):
                    _open_advanced_section(context, str(w.get("server_key") or data["key"]), str(w.get("advanced_section") or "general"))
                else:
                    w["step"] = "region"
                    _wizard_set(context, w)
                    _render_step_prompt(context, lang, "region", data)
                return
            if w["step"] == "target":
                if w.get("edit_single"):
                    _open_advanced_section(context, str(w.get("server_key") or data["key"]), str(w.get("advanced_section") or "general"))
                else:
                    w["step"] = "transport"
                    _wizard_set(context, w)
                    _wizard_edit(context, t(lang, "admin.wizard.server_choose_transport"), _transport_markup(lang))
                return
            if w["step"] == "public_host":
                if w.get("edit_single"):
                    _open_advanced_section(context, str(w.get("server_key") or data["key"]), str(w.get("advanced_section") or "general"))
                else:
                    if data["transport"] == "local":
                        w["step"] = "transport"
                        _wizard_set(context, w)
                        _wizard_edit(context, t(lang, "admin.wizard.server_choose_transport"), _transport_markup(lang))
                    else:
                        w["step"] = "target"
                        _wizard_set(context, w)
                        _render_step_prompt(context, lang, "target", data)
                return
            if w["step"] == "protocols":
                if w.get("edit_single"):
                    _open_advanced_section(context, str(w.get("server_key") or data["key"]), str(w.get("advanced_section") or "general"))
                else:
                    w["step"] = "public_host"
                    _wizard_set(context, w)
                    _render_step_prompt(context, lang, "public_host", data)
                return
            if w["step"] in {"notes", "xray_host", "xray_sni", "xray_fp", "xray_tcp_port", "xray_xhttp_port", "awg_public_host", "awg_port", "awg_iface", "awg_i1_preset"}:
                section = str(w.get("advanced_section") or _advanced_section_for_field(str(w["step"])))
                server_key = str(w.get("server_key") or data["key"])
                _open_advanced_section(context, server_key, section)
                return
        return

    if payload == "start:create":
        _start_create_flow(context, w, lang)
        return

    if payload == "start:create_local":
        _start_create_flow(context, w, lang, transport="local")
        return

    if payload == "start:create_remote":
        _start_create_flow(context, w, lang, transport="ssh")
        return

    if payload == "start:edit":
        servers = list_servers(include_disabled=True)
        w["mode"] = "edit"
        w["step"] = "pick"
        _wizard_set(context, w)
        _wizard_edit(context, t(lang, "admin.wizard.choose_server"), _pick_server_markup(servers, lang))
        return

    if payload.startswith("edit:"):
        _open_advanced_menu(context, payload.split(":", 1)[1])
        return

    if payload.startswith("pick:"):
        server_key = payload.split(":", 1)[1]
        server = get_server(server_key)
        if not server:
            _wizard_edit(context, t(lang, "admin.wizard.server_not_found"), kb_back_menu(lang))
            return
        w["server_key"] = server_key
        w["data"] = _load_server_into_data(server)
        w["step"] = "advanced"
        _wizard_set(context, w)
        _open_advanced_menu(context, server_key)
        return

    if payload.startswith("bootmenu:"):
        _open_bootstrap_menu(context, payload.split(":", 1)[1])
        return

    if payload.startswith("cleanupmenu:"):
        _open_full_cleanup_menu(context, payload.split(":", 1)[1])
        return

    if payload.startswith("bootmode:"):
        _, action, server_key = payload.split(":", 2)
        _open_bootstrap_mode(context, server_key, action)
        return

    if payload.startswith("cleanuprun:"):
        _, mode, server_key = payload.split(":", 2)
        stop_progress = _start_progress_animation(context, t(lang, "admin.wizard.full_cleanup"))
        rc, out = full_cleanup_server(server_key, remove_ssh_key=(mode == "runtime_ssh"))
        stop_progress()
        _wizard_edit(context, _action_result_text(t(lang, "admin.wizard.full_cleanup"), rc, out, server_key, lang), _server_card_markup(server_key, lang))
        return

    if payload.startswith("bootrun:"):
        _, action, mode, server_key = payload.split(":", 3)
        preserve_config = mode == "preserve"
        action_title = {
            "bootstrap": t(lang, "admin.wizard.bootstrap"),
            "reinstall": t(lang, "admin.wizard.reinstall"),
            "delete": t(lang, "admin.wizard.delete_runtime"),
        }.get(action, t(lang, "admin.wizard.work"))
        stop_progress = _start_progress_animation(context, action_title)
        if action == "bootstrap":
            rc, out = bootstrap_server(server_key, preserve_config=preserve_config)
            stop_progress()
            _wizard_edit(context, _action_result_text(t(lang, "admin.wizard.bootstrap"), rc, out, server_key, lang), _server_card_markup(server_key, lang))
            return
        if action == "reinstall":
            rc, out = reinstall_server(server_key, preserve_config=preserve_config)
            stop_progress()
            _wizard_edit(context, _action_result_text(t(lang, "admin.wizard.reinstall"), rc, out, server_key, lang), _server_card_markup(server_key, lang))
            return
        if action == "delete":
            rc, out = delete_server_runtime(server_key, preserve_config=preserve_config)
            stop_progress()
            _wizard_edit(context, _action_result_text(t(lang, "admin.wizard.delete_runtime"), rc, out, server_key, lang), _server_card_markup(server_key, lang))
            return
        stop_progress()

    if payload.startswith("action:"):
        _, action, server_key = payload.split(":", 2)
        if action == "metrics":
            stop_progress = _start_progress_animation(context, t(lang, "admin.wizard.server_metrics"))
            rc, out = show_server_metrics(server_key)
            stop_progress()
            _wizard_edit(context, _action_result_text(t(lang, "admin.wizard.server_metrics"), rc, out, server_key, lang), _metrics_result_markup(server_key, lang))
            return
        if action == "probe":
            stop_progress = _start_progress_animation(context, t(lang, "admin.wizard.probe"))
            rc, out = probe_server(server_key)
            stop_progress()
            _wizard_edit(context, _action_result_text(t(lang, "admin.wizard.probe"), rc, out, server_key, lang), _probe_result_markup(server_key, lang))
            return
        if action == "checkports":
            stop_progress = _start_progress_animation(context, t(lang, "admin.wizard.check_ports"))
            rc, out = check_server_ports(server_key)
            stop_progress()
            _wizard_edit(context, _action_result_text(t(lang, "admin.wizard.check_ports"), rc, out, server_key, lang), _server_card_markup(server_key, lang))
            return
        if action == "openports":
            stop_progress = _start_progress_animation(context, t(lang, "admin.wizard.open_ports"))
            rc, out = open_server_ports(server_key)
            stop_progress()
            _wizard_edit(context, _action_result_text(t(lang, "admin.wizard.open_ports"), rc, out, server_key, lang), _server_card_markup(server_key, lang))
            return
        if action == "installdocker":
            stop_progress = _start_progress_animation(context, t(lang, "admin.wizard.install_docker"))
            rc, out = install_server_docker(server_key)
            stop_progress()
            _wizard_edit(context, _action_result_text(t(lang, "admin.wizard.install_docker"), rc, out, server_key, lang), _server_card_markup(server_key, lang))
            return
        if action == "syncenv":
            stop_progress = _start_progress_animation(context, t(lang, "admin.wizard.sync_env"))
            rc, out = sync_server_node_env(server_key)
            stop_progress()
            _wizard_edit(context, _action_result_text(t(lang, "admin.wizard.sync_env"), rc, out, server_key, lang), _server_card_markup(server_key, lang))
            return
        if action == "syncxray":
            stop_progress = _start_progress_animation(context, t(lang, "admin.wizard.sync_xray"))
            rc, out = sync_xray_server_settings(server_key)
            stop_progress()
            _wizard_edit(context, _action_result_text(t(lang, "admin.wizard.sync_xray"), rc, out, server_key, lang), _server_card_markup(server_key, lang))
            return
        if action == "awgentropy":
            stop_progress = _start_progress_animation(context, t(lang, "admin.wizard.awg_entropy"))
            rc, out = show_awg_entropy(server_key)
            stop_progress()
            _wizard_edit(context, _action_result_text(t(lang, "admin.wizard.awg_entropy"), rc, out, server_key, lang), _server_card_markup(server_key, lang))
            return
        if action == "awgregen":
            stop_progress = _start_progress_animation(context, t(lang, "admin.wizard.awg_regen_entropy"))
            rc, out = regenerate_awg_entropy(server_key)
            stop_progress()
            _wizard_edit(context, _action_result_text(t(lang, "admin.wizard.awg_regen_entropy"), rc, out, server_key, lang), _server_card_markup(server_key, lang))
            return
        if action == "reconcile":
            stop_progress = _start_progress_animation(context, t(lang, "admin.wizard.reconcile"))
            rc, out = reconcile_xray_server_state(server_key)
            stop_progress()
            _wizard_edit(context, _action_result_text(t(lang, "admin.wizard.reconcile"), rc, out, server_key, lang), _server_card_markup(server_key, lang))
            return

    if payload.startswith("transport:"):
        data["transport"] = payload.split(":", 1)[1]
        if w["mode"] == "edit" and w.get("edit_single"):
            if data["transport"] == "local":
                data["target"] = ""
            server, err = _persist_edited_server(w, lang)
            if err or not server:
                _wizard_edit(context, err or t(lang, "admin.wizard.server_not_found"), kb_back_menu(lang))
                return
            server_key = server.key
            section = str(w.get("advanced_section") or "general")
            _wizard_set(context, w)
            _open_advanced_section(context, server_key, section)
            return
        w["step"] = "target"
        _wizard_set(context, w)
        if data["transport"] == "local":
            data["target"] = ""
            _wizard_edit(
                context,
                t(lang, "admin.wizard.server_enter_public_host_local"),
                _step_nav_markup(lang, next_payload=f"{CB_SRV}next"),
            )
            w["step"] = "public_host"
            _wizard_set(context, w)
            return
        _render_step_prompt(context, lang, "target", data)
        return

    if payload.startswith("protocol:"):
        item = payload.split(":", 1)[1]
        selected = data["protocol_kinds"]
        if item == "done":
            if not selected:
                _wizard_edit(context, t(lang, "admin.wizard.server_protocol_required"), _protocol_markup(selected, lang))
                return
            if w["mode"] == "edit" and w.get("edit_single"):
                server, err = _persist_edited_server(w, lang)
                if err or not server:
                    _wizard_edit(context, err or t(lang, "admin.wizard.server_not_found"), kb_back_menu(lang))
                    return
                server_key = server.key
                section = str(w.get("advanced_section") or "general")
                _wizard_set(context, w)
                _open_advanced_section(context, server_key, section)
            else:
                _wizard_edit(context, _summary_text(data, editing=w["mode"] == "edit", lang=lang), _summary_markup(lang))
            return
        if item in selected:
            selected.remove(item)
        else:
            selected.add(item)
        _wizard_set(context, w)
        _wizard_edit(context, t(lang, "admin.wizard.server_choose_protocols"), _protocol_markup(selected, lang))
        return

    if payload.startswith("awgpreset:"):
        data["awg_i1_preset"] = payload.split(":", 1)[1]
        server_key = str(w.get("server_key") or data["key"])
        if w["mode"] == "edit":
            server, err = _persist_edited_server(w, lang)
            if err or not server:
                _wizard_edit(context, err or t(lang, "admin.wizard.server_not_found"), kb_back_menu(lang))
                return
            server_key = server.key
        w["step"] = "advanced_awg"
        w["advanced_section"] = "awg"
        _wizard_set(context, w)
        _open_advanced_section(context, server_key, "awg")
        return

    if payload.startswith("editfield:"):
        field = payload.split(":", 1)[1]
        w["mode"] = "edit"
        w["edit_single"] = True
        w["advanced_section"] = _advanced_section_for_field(field)
        if field == "transport":
            w["step"] = "transport"
            _wizard_set(context, w)
            _wizard_edit(context, t(lang, "admin.wizard.server_choose_transport"), _transport_markup(lang))
            return
        if field == "protocols":
            w["step"] = "protocols"
            _wizard_set(context, w)
            _wizard_edit(context, t(lang, "admin.wizard.server_choose_protocols"), _protocol_markup(data["protocol_kinds"], lang))
            return
        if field == "awg_i1_preset":
            w["step"] = "awg_i1_preset"
            _wizard_set(context, w)
            prompt = t(lang, "admin.wizard.awg_preset_prompt")
            _wizard_edit(context, prompt, _awg_preset_markup(lang))
            return
        if field == "notes":
            w["step"] = "notes"
            _wizard_set(context, w)
            prompt = "Введи заметки для сервера или `.` чтобы оставить как есть." if lang == "ru" else "Enter server notes or `.` to keep the current value."
            _wizard_edit(context, prompt, _step_nav_markup(lang, next_payload=f"{CB_SRV}next", next_label_key="admin.wizard.save_now"))
            return
        field_prompts = {
            "title": t(lang, "admin.wizard.server_create_title"),
            "flag": t(lang, "admin.wizard.server_create_flag", flag=data["flag"]),
            "region": t(lang, "admin.wizard.server_create_region"),
            "target": t(lang, "admin.wizard.server_enter_target"),
            "public_host": t(lang, "admin.wizard.server_enter_public_host"),
            "xray_host": t(lang, "admin.wizard.prompt_xray_host"),
            "xray_sni": "Введи Xray dest/SNI. Например: `www.cloudflare.com`" if lang == "ru" else "Enter Xray dest/SNI. Example: `www.cloudflare.com`",
            "xray_fp": "Введи Xray uTLS fingerprint. Например: `chrome`" if lang == "ru" else "Enter the Xray uTLS fingerprint. Example: `chrome`",
            "xray_tcp_port": t(lang, "admin.wizard.prompt_xray_tcp_port"),
            "xray_xhttp_port": t(lang, "admin.wizard.prompt_xray_xhttp_port"),
            "awg_public_host": t(lang, "admin.wizard.prompt_awg_host"),
            "awg_port": t(lang, "admin.wizard.prompt_awg_port"),
            "awg_iface": t(lang, "admin.wizard.prompt_awg_iface"),
        }
        if field in field_prompts:
            w["step"] = field
            _wizard_set(context, w)
            _wizard_edit(context, field_prompts[field], _step_nav_markup(lang, next_payload=f"{CB_SRV}next", next_label_key="admin.wizard.save_now"))
            return

    if payload == "editsave":
        server, err = _persist_edited_server(w, lang)
        if err or not server:
            _wizard_edit(context, err or t(lang, "admin.wizard.server_not_found"), kb_back_menu(lang))
            return
        _wizard_set(context, w)
        saved = t(lang, "admin.wizard.server_saved_inline")
        _wizard_edit(context, f"{saved}\n\n{_advanced_menu_text(server, lang)}", _advanced_menu_markup(server.key, lang))
        return

    if payload == "save":
        target = data["target"].strip()
        public_host = data["public_host"].strip()
        try:
            sanitized_key = validate_server_key(data["key"])
            sanitized = {
                "title": validate_server_field("title", data["title"]),
                "flag": validate_server_field("flag", data["flag"]),
                "region": validate_server_field("region", data["region"]),
                "transport": validate_server_field("transport", data["transport"]),
                "public_host": validate_server_field("public_host", public_host or target.split("@")[-1]),
                "ssh_host": validate_server_field("ssh_host", target or ""),
                "protocol_kinds": validate_server_field("protocol_kinds", sorted(data["protocol_kinds"])),
                "awg_i1_preset": validate_server_field("awg_i1_preset", data.get("awg_i1_preset") or "quic"),
                "xray_sni": validate_server_field("xray_sni", data.get("xray_sni") or ""),
                "xray_fp": validate_server_field("xray_fp", data.get("xray_fp") or "chrome"),
            }
        except ValueError as exc:
            _wizard_edit(context, str(exc), kb_back_menu(lang))
            return
        server = upsert_server(
            key=sanitized_key,
            title=sanitized["title"],
            flag=sanitized["flag"],
            region=sanitized["region"],
            transport=sanitized["transport"],
            protocol_kinds=sanitized["protocol_kinds"],
            public_host=sanitized["public_host"],
            ssh_host=sanitized["ssh_host"] or None,
            bootstrap_state="new" if w["mode"] == "create" else "edited",
        )
        if sanitized["awg_i1_preset"] != "quic":
            server = update_server_fields(server.key, awg_i1_preset=sanitized["awg_i1_preset"])
        if sanitized["xray_sni"] or sanitized["xray_fp"]:
            server = update_server_fields(
                server.key,
                xray_sni=sanitized["xray_sni"],
                xray_fp=sanitized["xray_fp"],
            )
        set_initial_setup_state("completed")
        w["mode"] = "menu"
        w["step"] = "menu"
        w["server_key"] = server.key
        w["data"] = _load_server_into_data(server)
        _wizard_set(context, w)
        _wizard_edit(
            context,
            t(
                lang,
                "admin.wizard.server_saved",
                flag=server.flag,
                title=server.title,
                server_key=server.key,
                transport=server.transport,
                protocols=", ".join(server.protocol_kinds),
            ),
            _server_card_markup(server.key, lang),
        )


def serverconfig_cmd(update: Update, context: CallbackContext) -> None:
    if not guard(update):
        return
    lang = get_locale_for_update(update)
    parts = (update.effective_message.text or "").strip().split()
    if len(parts) != 2:
        update.effective_message.reply_text(t(lang, "admin.cmd.usage_serverconfig"))
        return
    server = get_server(parts[1])
    if not server:
        update.effective_message.reply_text(t(lang, "admin.cmd.server_not_found"), reply_markup=kb_back_menu(lang))
        return
    text = (
        _server_card_text(server, lang)
        + f"\n\n{t(lang, 'admin.cmd.field_edit_hint')}\n"
        + f"/setserverfield {server.key} <field> <value>"
    )
    update.effective_message.reply_text(text, parse_mode=None, reply_markup=_server_card_markup(server.key, lang))


def setserverfield_cmd(update: Update, context: CallbackContext) -> None:
    if not guard(update):
        return
    lang = get_locale_for_update(update)
    from services.server_registry import update_server_fields

    parts = (update.effective_message.text or "").strip().split(maxsplit=3)
    if len(parts) != 4:
        update.effective_message.reply_text(t(lang, "admin.cmd.usage_setserverfield"))
        return
    key, field, value = parts[1], parts[2], parts[3]
    int_fields = {"ssh_port", "xray_tcp_port", "xray_xhttp_port", "awg_port"}
    runtime_fields = {
        "protocol_kinds",
        "public_host",
        "xray_config_path",
        "xray_service_name",
        "xray_host",
        "xray_sni",
        "xray_pbk",
        "xray_short_id",
        "xray_fp",
        "xray_flow",
        "xray_tcp_port",
        "xray_xhttp_port",
        "xray_xhttp_path_prefix",
        "awg_config_path",
        "awg_iface",
        "awg_public_host",
        "awg_port",
    }
    try:
        if field in int_fields:
            value_obj: object = int(value)
        elif field == "protocol_kinds":
            value_obj = [item.strip() for item in value.split(",") if item.strip()]
        elif field == "enabled":
            value_obj = value.lower() in {"1", "true", "yes", "on"}
        else:
            value_obj = value
        value_obj = validate_server_field(field, value_obj)
    except ValueError as exc:
        update.effective_message.reply_text(str(exc), reply_markup=kb_back_menu(lang))
        return
    update_fields = {field: value_obj}
    if field in runtime_fields:
        update_fields["bootstrap_state"] = "edited"
    server = update_server_fields(key, **update_fields)
    text = t(lang, "admin.cmd.field_updated", field=_md(field), value=_md(value), server=_md(server.key))
    if field in runtime_fields:
        text = f"{text}\n\n{t(lang, 'admin.cmd.field_updated_runtime_note')}"
    update.effective_message.reply_text(
        text,
        parse_mode=None,
        reply_markup=kb_back_menu(lang),
    )


def syncnodeenv_cmd(update: Update, context: CallbackContext) -> None:
    if not guard(update):
        return
    lang = get_locale_for_update(update)
    parts = (update.effective_message.text or "").strip().split()
    if len(parts) != 2:
        update.effective_message.reply_text(t(lang, "admin.cmd.usage_syncnodeenv"))
        return
    code, out = sync_server_node_env(parts[1])
    if code != 0:
        update.effective_message.reply_text(
            t(lang, "admin.cmd.sync_error", output=out[-1500:]),
            parse_mode=PARSE_MODE,
            reply_markup=kb_back_menu(lang),
        )
        return
    update.effective_message.reply_text(
        t(lang, "admin.cmd.sync_ok", output=out),
        parse_mode=PARSE_MODE,
        reply_markup=kb_back_menu(lang),
    )
