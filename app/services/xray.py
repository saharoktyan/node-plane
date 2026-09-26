"""Xray client identifiers and VLESS link rendering.

Runtime mutations and observations go through the Rust driver.
"""

from __future__ import annotations

import json
import secrets
from typing import Optional
from urllib.parse import quote

from services.server_registry import get_server


def get_uuid_local(name: str) -> Optional[str]:
    from services.profile_state import get_profile

    record = get_profile(name)
    value = record.get("uuid") if isinstance(record, dict) else None
    return value if isinstance(value, str) and value.strip() else None


def get_short_id_local(name: str, server_key: Optional[str] = None) -> Optional[str]:
    from services.profile_state import get_profile

    record = get_profile(name)
    xray = record.get("xray") if isinstance(record, dict) else None
    if isinstance(xray, dict):
        if server_key:
            server_short_ids = xray.get("server_short_ids")
            if isinstance(server_short_ids, dict):
                short_id = server_short_ids.get(server_key)
                if isinstance(short_id, str) and short_id.strip():
                    return short_id.strip()
        short_id = xray.get("short_id")
        if isinstance(short_id, str) and short_id.strip():
            return short_id.strip()
    return None


def generate_short_id() -> str:
    return secrets.token_hex(8)


def build_vless_link_transport(name: str, uuid: str, transport: str, server_key: str) -> str:
    server = get_server(server_key)
    if not server:
        raise KeyError(server_key)
    if server.bootstrap_state == "edited":
        raise ValueError(f"Server {server_key} has unapplied settings")

    # Verify Reality settings through the driver before issuing a link, so a
    # previously provisioned profile does not retain a stale server key/SID.
    from services.node_driver import get_node_driver

    try:
        driver = get_node_driver()
        provisioned = driver.ensure_profile_on_node(server_key, name, ["xray"], xray_uuid=uuid)
        if provisioned.status != "SUCCEEDED":
            raise ValueError("Xray profile could not be restored on the node")
        operation = driver.sync_xray(server_key)
    except Exception as exc:
        raise ValueError(f"Could not verify current Xray settings: {exc}") from exc
    if operation.status != "SUCCEEDED":
        raise ValueError(f"Could not verify current Xray settings: {operation.progress_message}")
    server = get_server(server_key)

    ready, reason = get_server_link_status(server_key)
    if not ready:
        raise ValueError(reason)

    short_id = server.xray_short_id or server.xray_sid
    path_prefix = server.xray_xhttp_path_prefix or "/assets"
    if transport == "xhttp":
        xhttp_extra = quote(json.dumps({"xmux": {"maxConcurrency": "16-32"}}, separators=(",", ":")), safe="")
        return (
            f"vless://{uuid}@{server.xray_host}:{server.xray_xhttp_port}"
            f"?encryption=none&security=reality&sni={server.xray_sni}"
            f"&fp={server.xray_fp}&pbk={server.xray_pbk}&sid={short_id}"
            f"&type=xhttp&path={quote(path_prefix, safe='')}&mode=auto&extra={xhttp_extra}"
            f"#{quote(f'VLESS {server.title} · {name} · XHTTP', safe='')}"
        )
    return (
        f"vless://{uuid}@{server.xray_host}:{server.xray_tcp_port}"
        f"?encryption=none&security=reality&sni={server.xray_sni}"
        f"&fp={server.xray_fp}&pbk={server.xray_pbk}&sid={short_id}"
        f"&type=tcp&flow={server.xray_flow}"
        f"#{quote(f'VLESS {server.title} · {name} · TCP', safe='')}"
    )


def get_server_link_status(server_key: str) -> tuple[bool, str]:
    server = get_server(server_key)
    if not server:
        return False, f"Server {server_key} not found"
    if server.bootstrap_state == "edited":
        return False, f"Server {server_key} has unapplied settings"

    missing = [field for field in ("xray_host", "xray_sni", "xray_pbk", "xray_sid") if not getattr(server, field)]
    if missing:
        return False, f"Xray link settings are incomplete for server {server_key}: {', '.join(missing)}"
    return True, "ok"
