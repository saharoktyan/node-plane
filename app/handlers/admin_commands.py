# app/handlers/admin_commands.py
from __future__ import annotations

import uuid as uuid_lib

from telegram import Update
from telegram.ext import CallbackContext

from config import PARSE_MODE
from i18n import get_locale_for_update, t
from services.app_settings import set_initial_setup_state
from services.node_driver import get_node_driver
from services.server_registry import list_servers, update_server_fields, upsert_server
from services.ssh_keys import render_public_key_guide
from services.traffic_usage import debug_awg_traffic_report, debug_profile_traffic_report, run_collect_traffic_once
from services.xray import debug_xray_telemetry_report
from services import xray as xray_svc
from services.profile_state import ensure_xray_caps, profile_store
from utils.security import redact_sensitive_text, validate_profile_name, validate_server_field, validate_server_key
from config import APP_VERSION

from .admin_common import guard, kb_back_menu


def _safe_output(value: str, limit: int = 1500) -> str:
    return redact_sensitive_text(value or "")[:limit]


def add_cmd(update: Update, context: CallbackContext) -> None:
    if not guard(update):
        return
    lang = get_locale_for_update(update)
    parts = (update.effective_message.text or "").strip().split()
    if len(parts) < 2:
        update.effective_message.reply_text(t(lang, "admin.cmd.add_usage"))
        return
    try:
        name = validate_profile_name(parts[1])
    except ValueError as exc:
        update.effective_message.reply_text(str(exc), reply_markup=kb_back_menu(lang))
        return
    uuid_val = str(uuid_lib.uuid4())

    update.effective_message.reply_text(t(lang, "admin.cmd.add_creating", name=name))
    default_server_key = next((server.key for server in list_servers() if server.enabled and "xray" in server.protocol_kinds), "")
    code, out, ensured_uuid, ensured_short_id = xray_svc.ensure_user(name, default_server_key, uuid_value=uuid_val)
    if code != 0 or not ensured_uuid:
        update.effective_message.reply_text(
            t(lang, "admin.cmd.error", output=_safe_output(out)),
            parse_mode=PARSE_MODE,
            reply_markup=kb_back_menu(lang),
        )
        return

    ensure_xray_caps(name, ensured_uuid)
    if ensured_short_id:
        from services.profile_state import set_xray_short_id

        set_xray_short_id(name, ensured_short_id, server_key=default_server_key)
    update.effective_message.reply_text(
        f"✅ {name}",
        parse_mode=PARSE_MODE,
        reply_markup=kb_back_menu(lang),
    )


def del_cmd(update: Update, context: CallbackContext) -> None:
    if not guard(update):
        return
    lang = get_locale_for_update(update)
    parts = (update.effective_message.text or "").strip().split()
    if len(parts) < 2:
        update.effective_message.reply_text(t(lang, "admin.cmd.del_usage"))
        return
    try:
        name = validate_profile_name(parts[1])
    except ValueError as exc:
        update.effective_message.reply_text(str(exc), reply_markup=kb_back_menu(lang))
        return
    code, out = xray_svc.delete_user(name)
    if code == 0:
        update.effective_message.reply_text(t(lang, "admin.cmd.deleted"), reply_markup=kb_back_menu(lang))
    else:
        update.effective_message.reply_text(
            t(lang, "admin.cmd.error", output=_safe_output(out)),
            parse_mode=PARSE_MODE,
            reply_markup=kb_back_menu(lang),
        )


def list_cmd(update: Update, context: CallbackContext) -> None:
    if not guard(update):
        return
    lang = get_locale_for_update(update)
    subs = profile_store.read()
    names = sorted(
        name
        for name, rec in subs.items()
        if not str(name).startswith("_") and isinstance(rec, dict) and rec.get("uuid")
    )
    if not names:
        update.effective_message.reply_text(t(lang, "admin.cmd.list_empty"), reply_markup=kb_back_menu(lang))
        return
    text = t(lang, "admin.cmd.xray_profiles") + "\n\n" + "\n".join(f"- `{name}`" for name in names)
    update.effective_message.reply_text(text[:3900], parse_mode=PARSE_MODE, reply_markup=kb_back_menu(lang))


def servers_cmd(update: Update, context: CallbackContext) -> None:
    if not guard(update):
        return
    lang = get_locale_for_update(update)
    servers = list_servers(include_disabled=True)
    if not servers:
        update.effective_message.reply_text(t(lang, "admin.cmd.no_servers"), reply_markup=kb_back_menu(lang))
        return
    lines = [t(lang, "admin.cmd.servers_title")]
    for server in servers:
        protocols = ", ".join(server.protocol_kinds) or "—"
        target = "local" if server.transport == "local" else (server.ssh_target or "ssh:?")
        lines.append(
            f"\n• `{server.key}` {server.flag} *{server.title}*"
            f"\n  region: `{server.region}`"
            f"\n  transport: `{server.transport}` ({target})"
            f"\n  protocols: `{protocols}`"
            f"\n  bootstrap: `{server.bootstrap_state}`"
        )
    update.effective_message.reply_text("\n".join(lines)[:3900], parse_mode=PARSE_MODE, reply_markup=kb_back_menu(lang))


def addserver_cmd(update: Update, context: CallbackContext) -> None:
    if not guard(update):
        return
    lang = get_locale_for_update(update)
    parts = (update.effective_message.text or "").strip().split()
    if len(parts) < 7:
        update.effective_message.reply_text(t(lang, "admin.cmd.addserver_usage"))
        return
    key, title, flag, region, transport, protocols = parts[1:7]
    target = parts[7] if len(parts) >= 8 else ""
    try:
        key = validate_server_key(key)
        title = str(validate_server_field("title", title))
        flag = str(validate_server_field("flag", flag))
        region = str(validate_server_field("region", region))
        transport = str(validate_server_field("transport", transport))
        protocol_values = validate_server_field("protocol_kinds", protocols.split(","))
        target_value = str(validate_server_field("ssh_host", target)) if transport == "ssh" else ""
        public_host = str(validate_server_field("public_host", target_value.split("@")[-1] if target_value else ""))
    except ValueError as exc:
        update.effective_message.reply_text(str(exc), reply_markup=kb_back_menu(lang))
        return
    server = upsert_server(
        key=key,
        title=title,
        flag=flag,
        region=region,
        transport=transport,
        protocol_kinds=protocol_values,
        public_host=public_host if transport == "ssh" else "",
        ssh_host=target_value if transport == "ssh" else None,
        bootstrap_state="new",
    )
    set_initial_setup_state("completed")
    update.effective_message.reply_text(
        t(lang, "admin.cmd.server_registered", server=server.key, flag=server.flag, title=server.title, transport=server.transport, protocols=", ".join(server.protocol_kinds) or "—"),
        parse_mode=PARSE_MODE,
        reply_markup=kb_back_menu(lang),
    )


def probeserver_cmd(update: Update, context: CallbackContext) -> None:
    if not guard(update):
        return
    lang = get_locale_for_update(update)
    parts = (update.effective_message.text or "").strip().split()
    if len(parts) != 2:
        update.effective_message.reply_text(t(lang, "admin.cmd.usage_probeserver"))
        return
    operation = get_node_driver().probe_node(parts[1])
    code = 0 if operation.status == "SUCCEEDED" else 1
    out = operation.progress_message
    if code != 0:
        update.effective_message.reply_text(
            t(lang, "admin.cmd.probe_error", output=_safe_output(out)),
            parse_mode=PARSE_MODE,
            reply_markup=kb_back_menu(lang),
        )
        return
    update.effective_message.reply_text(
        t(lang, "admin.cmd.probe_ok", output=_safe_output(out)),
        parse_mode=PARSE_MODE,
        reply_markup=kb_back_menu(lang),
    )


def sshkey_cmd(update: Update, context: CallbackContext) -> None:
    if not guard(update):
        return
    lang = get_locale_for_update(update)
    ok, text = render_public_key_guide(lang)
    if not ok:
        update.effective_message.reply_text(
            t(lang, "admin.cmd.sshkey_error", output=text[-1500:]),
            parse_mode=PARSE_MODE,
            reply_markup=kb_back_menu(lang),
        )
        return
    update.effective_message.reply_text(text[:3900], parse_mode=None, reply_markup=kb_back_menu(lang))


def bootstrapserver_cmd(update: Update, context: CallbackContext) -> None:
    if not guard(update):
        return
    lang = get_locale_for_update(update)
    parts = (update.effective_message.text or "").strip().split()
    if len(parts) != 2:
        update.effective_message.reply_text(t(lang, "admin.cmd.usage_bootstrapserver"))
        return
    key = parts[1]
    update.effective_message.reply_text(t(lang, "admin.cmd.bootstrap_running", server=key), parse_mode=PARSE_MODE)
    operation = get_node_driver().bootstrap_node(key)
    code = 0 if operation.status == "SUCCEEDED" else 1
    out = operation.progress_message
    if code != 0:
        update.effective_message.reply_text(
            t(lang, "admin.cmd.bootstrap_error", output=_safe_output(out)),
            parse_mode=PARSE_MODE,
            reply_markup=kb_back_menu(lang),
        )
        return
    update.effective_message.reply_text(
        t(lang, "admin.cmd.bootstrap_ok", output=_safe_output(out, limit=3000)),
        parse_mode=PARSE_MODE,
        reply_markup=kb_back_menu(lang),
    )


def setxrayserver_cmd(update: Update, context: CallbackContext) -> None:
    if not guard(update):
        return
    lang = get_locale_for_update(update)
    parts = (update.effective_message.text or "").strip().split()
    if len(parts) < 6:
        update.effective_message.reply_text(t(lang, "admin.cmd.usage_setxrayserver"))
        return

    try:
        key = validate_server_key(parts[1])
        host = str(validate_server_field("xray_host", parts[2]))
        sni = str(validate_server_field("xray_sni", parts[3]))
        pbk = str(validate_server_field("xray_pbk", parts[4]))
        sid = str(validate_server_field("xray_sid", parts[5]))
        short_id = str(validate_server_field("xray_short_id", parts[6] if len(parts) >= 7 else sid))
        tcp_port = int(validate_server_field("xray_tcp_port", int(parts[7]) if len(parts) >= 8 else 443))
        xhttp_port = int(validate_server_field("xray_xhttp_port", int(parts[8]) if len(parts) >= 9 else 8443))
        path_prefix = str(validate_server_field("xray_xhttp_path_prefix", parts[9] if len(parts) >= 10 else "/assets"))
        fp = str(validate_server_field("xray_fp", parts[10] if len(parts) >= 11 else "chrome"))
    except ValueError as exc:
        update.effective_message.reply_text(str(exc), reply_markup=kb_back_menu(lang))
        return

    server = update_server_fields(
        key,
        xray_host=host,
        xray_sni=sni,
        xray_pbk=pbk,
        xray_sid=sid,
        xray_short_id=short_id,
        xray_fp=fp,
        xray_tcp_port=tcp_port,
        xray_xhttp_port=xhttp_port,
        xray_xhttp_path_prefix=path_prefix,
    )
    update.effective_message.reply_text(
        t(lang, "admin.cmd.xray_settings_updated", server=server.key, host=server.xray_host, sni=server.xray_sni, tcp=server.xray_tcp_port, xhttp=server.xray_xhttp_port),
        parse_mode=PARSE_MODE,
        reply_markup=kb_back_menu(lang),
    )


def syncxrayserver_cmd(update: Update, context: CallbackContext) -> None:
    if not guard(update):
        return
    lang = get_locale_for_update(update)
    parts = (update.effective_message.text or "").strip().split()
    if len(parts) != 2:
        update.effective_message.reply_text(t(lang, "admin.cmd.usage_syncxrayserver"))
        return
    key = parts[1]
    operation = get_node_driver().sync_xray(key)
    code = 0 if operation.status == "SUCCEEDED" else 1
    out = operation.progress_message
    if code != 0:
        update.effective_message.reply_text(
            t(lang, "admin.cmd.sync_xray_error", output=_safe_output(out)),
            parse_mode=PARSE_MODE,
            reply_markup=kb_back_menu(lang),
        )
        return
    update.effective_message.reply_text(
        t(lang, "admin.cmd.sync_xray_ok", server=key, output=_safe_output(out, limit=3000)),
        parse_mode=PARSE_MODE,
        reply_markup=kb_back_menu(lang),
    )


def diag_cmd(update: Update, context: CallbackContext) -> None:
    if not guard(update):
        return
    lang = get_locale_for_update(update)
    parts = (update.effective_message.text or "").strip().split()
    if len(parts) >= 3 and parts[1].lower() == "xray":
        server_key = parts[2]
        code, out = debug_xray_telemetry_report(server_key)
        if code != 0:
            update.effective_message.reply_text(
                t(lang, "admin.cmd.xray_diag_error", output=_safe_output(out, limit=3000)),
                parse_mode=PARSE_MODE,
                reply_markup=kb_back_menu(lang),
            )
            return
        update.effective_message.reply_text(
            t(lang, "admin.cmd.xray_diag_ok", server=server_key, output=_safe_output(out, limit=3500)),
            parse_mode=PARSE_MODE,
            reply_markup=kb_back_menu(lang),
        )
        return
    if len(parts) >= 3 and parts[1].lower() == "awg":
        server_key = parts[2]
        code, out = debug_awg_traffic_report(server_key)
        if code != 0:
            update.effective_message.reply_text(
                t(lang, "admin.cmd.awg_diag_error", output=_safe_output(out, limit=3000)),
                parse_mode=PARSE_MODE,
                reply_markup=kb_back_menu(lang),
            )
            return
        update.effective_message.reply_text(
            t(lang, "admin.cmd.awg_diag_ok", server=server_key, output=_safe_output(out, limit=3500)),
            parse_mode=PARSE_MODE,
            reply_markup=kb_back_menu(lang),
        )
        return
    if len(parts) >= 4 and parts[1].lower() == "traffic":
        profile_name = parts[2].lstrip("@")
        protocol_kind = parts[3].lower()
        code, out = debug_profile_traffic_report(profile_name, protocol_kind)
        if code != 0:
            update.effective_message.reply_text(
                t(lang, "admin.cmd.traffic_diag_error", output=_safe_output(out, limit=3000)),
                parse_mode=PARSE_MODE,
                reply_markup=kb_back_menu(lang),
            )
            return
        update.effective_message.reply_text(
            t(lang, "admin.cmd.traffic_diag_ok", name=profile_name, protocol=protocol_kind, output=_safe_output(out, limit=3500)),
            parse_mode=PARSE_MODE,
            reply_markup=kb_back_menu(lang),
        )
        return
    if len(parts) >= 2:
        update.effective_message.reply_text(
            t(lang, "admin.cmd.usage_diag"),
            reply_markup=kb_back_menu(lang),
        )
        return
    servers = list_servers(include_disabled=True)
    xray_ready = 0
    awg_ready = 0
    for server in servers:
        if "xray" in server.protocol_kinds and server.xray_pbk and server.xray_sni and server.xray_sid:
            xray_ready += 1
        if "awg" in server.protocol_kinds and server.bootstrap_state == "bootstrapped":
            awg_ready += 1
    text = (
        f"{t(lang, 'admin.cmd.diag_title')}\n\n"
        f"version: {APP_VERSION}\n"
        f"servers_total: {len(servers)}\n"
        f"xray_ready: {xray_ready}\n"
        f"awg_ready: {awg_ready}\n"
    )
    update.effective_message.reply_text(text, parse_mode=None, reply_markup=kb_back_menu(lang))


def collecttraffic_cmd(update: Update, context: CallbackContext) -> None:
    if not guard(update):
        return
    lang = get_locale_for_update(update)
    code, out = run_collect_traffic_once()
    if code != 0:
        update.effective_message.reply_text(
            t(lang, "admin.cmd.collect_traffic_error", output=_safe_output(out, limit=3000)),
            parse_mode=PARSE_MODE,
            reply_markup=kb_back_menu(lang),
        )
        return
    update.effective_message.reply_text(
        t(lang, "admin.cmd.collect_traffic_ok", output=_safe_output(out, limit=3000)),
        parse_mode=PARSE_MODE,
        reply_markup=kb_back_menu(lang),
    )
