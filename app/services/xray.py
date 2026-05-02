# app/services/xray.py
from __future__ import annotations

import logging
import secrets
import shlex
import threading
import uuid as uuid_lib
from typing import Any, List, Optional, Tuple
from urllib.parse import quote

from services.server_bootstrap import XRAY_ENABLE_STATS_SCRIPT, XRAY_TRAFFIC_SCRIPT
from services.server_registry import get_server, list_servers
from services.server_runtime import run_local_command, run_server_command, write_server_file
from utils.security import validate_profile_name, validate_server_key

log = logging.getLogger("xray")

_cache: dict[str, dict[str, object]] = {}
_telemetry_ready: set[str] = set()
_cache_lock = threading.Lock()
_telemetry_lock = threading.Lock()


def run_local(cmd: str, timeout: int = 60) -> Tuple[int, str]:
    return run_local_command(cmd, timeout=timeout)


def _cache_get(server_key: str, ttl: float) -> Optional[Tuple[int, List[str], str]]:
    import time

    with _cache_lock:
        item = _cache.get(server_key)
    if not item:
        return None
    if time.time() - float(item["ts"]) >= ttl:
        return None
    return int(item["code"]), list(item["names"]), str(item["raw"])


def _cache_set(server_key: str, code: int, names: List[str], raw: str) -> None:
    import time

    with _cache_lock:
        _cache[server_key] = {"ts": time.time(), "code": code, "names": list(names), "raw": raw}


def _default_xray_server_key() -> Optional[str]:
    for server in list_servers():
        if server.enabled and "xray" in server.protocol_kinds:
            return server.key
    return None


def get_uuid_local(name: str) -> Optional[str]:
    from services.profile_state import get_profile

    rec = get_profile(name)
    uuid_val = rec.get("uuid") if isinstance(rec, dict) else None
    return str(uuid_val) if isinstance(uuid_val, str) and uuid_val.strip() else None


def get_short_id_local(name: str, server_key: Optional[str] = None) -> Optional[str]:
    from services.profile_state import get_profile

    rec = get_profile(name)
    xray = rec.get("xray") if isinstance(rec, dict) else None
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


def add_user(
    name: str,
    server_key: Optional[str] = None,
    uuid_value: Optional[str] = None,
    short_id: Optional[str] = None,
) -> Tuple[int, str]:
    try:
        name = validate_profile_name(name)
        if server_key:
            server_key = validate_server_key(server_key)
    except ValueError as exc:
        return 1, str(exc)
    server_key = server_key or _default_xray_server_key()
    if not server_key:
        return 1, "No Xray servers are registered"
    server = get_server(server_key)
    if not server:
        return 1, f"Server {server_key} not found"

    if uuid_value:
        cmd = f"/opt/node-plane-runtime/xray-add-user-existing.sh {shlex.quote(name)} {shlex.quote(uuid_value)}"
        if short_id:
            cmd += f" {shlex.quote(short_id)}"
    else:
        cmd = f"echo {shlex.quote(name)} | /opt/node-plane-runtime/xray-add-user.sh"
    return run_server_command(server, cmd, timeout=120)


def list_users(server_key: Optional[str] = None) -> Tuple[int, List[str], str]:
    server_key = server_key or _default_xray_server_key()
    if not server_key:
        return 1, [], "No Xray servers are registered"
    server = get_server(server_key)
    if not server:
        return 1, [], f"Server {server_key} not found"
    code, out = run_server_command(server, "/opt/node-plane-runtime/xray-list-users.sh", timeout=60)
    if code != 0:
        return code, [], out
    lines = out.strip().splitlines()
    if not lines:
        return 0, [], out
    names: List[str] = []
    for line in lines[1:]:
        parts = line.split()
        if parts:
            names.append(parts[0])
    return 0, names, out


def list_user_records(server_key: Optional[str] = None) -> Tuple[int, List[dict[str, Any]], str]:
    server_key = server_key or _default_xray_server_key()
    if not server_key:
        return 1, [], "No Xray servers are registered"
    server = get_server(server_key)
    if not server:
        return 1, [], f"Server {server_key} not found"
    code, out = run_server_command(server, "/opt/node-plane-runtime/xray-list-users.sh", timeout=60)
    if code != 0:
        return code, [], out
    lines = out.strip().splitlines()
    if not lines:
        return 0, [], out
    items: List[dict[str, Any]] = []
    for line in lines[1:]:
        parts = line.split()
        if len(parts) >= 2:
            items.append({"name": parts[0], "uuid": parts[1]})
        elif parts:
            items.append({"name": parts[0], "uuid": None})
    return 0, items, out


def ensure_xray_telemetry(server_key: str) -> Tuple[int, str]:
    with _telemetry_lock:
        if server_key in _telemetry_ready:
            return 0, "ok"
    server = get_server(server_key)
    if not server:
        return 1, f"Server {server_key} not found"
    for path, content in (
        ("/opt/node-plane-runtime/xray-enable-stats.sh", XRAY_ENABLE_STATS_SCRIPT),
        ("/opt/node-plane-runtime/xray-list-traffic.sh", XRAY_TRAFFIC_SCRIPT),
    ):
        code, out = write_server_file(server, path, content, mode="0755")
        if code != 0:
            return code, out
    code, out = run_server_command(server, "/opt/node-plane-runtime/xray-enable-stats.sh", timeout=120)
    if code == 0:
        with _telemetry_lock:
            _telemetry_ready.add(server_key)
    return code, out


def list_xray_user_transfers(server_key: str) -> Tuple[int, List[dict[str, Any]], str]:
    server = get_server(server_key)
    if not server:
        return 1, [], f"Server {server_key} not found"

    code, _out = ensure_xray_telemetry(server_key)
    if code != 0:
        return code, [], _out

    code, out = run_server_command(server, "/opt/node-plane-runtime/xray-list-traffic.sh", timeout=120)
    if code != 0:
        return code, [], out
    try:
        import json

        items = json.loads(out.strip() or "[]")
    except Exception:
        return 1, [], f"Could not parse xray traffic output:\n{out[-1500:]}"
    if not isinstance(items, list):
        return 1, [], f"Unexpected xray traffic payload:\n{out[-1500:]}"
    return 0, [item for item in items if isinstance(item, dict)], out


def debug_xray_telemetry_report(server_key: str) -> Tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Server {server_key} not found"

    lines: list[str] = [f"Xray telemetry debug: server={server_key}"]
    code, out = ensure_xray_telemetry(server_key)
    lines.append(f"ensure_telemetry_rc={code}")
    if out:
        lines.append("ensure_telemetry_output:")
        lines.append(out[-1500:])

    run_code, run_out = run_server_command(server, "/opt/node-plane-runtime/xray-list-traffic.sh", timeout=120)
    lines.append(f"traffic_script_rc={run_code}")
    if run_out:
        lines.append("traffic_script_output:")
        lines.append(run_out[-3000:])

    parsed_code, records, _raw = list_xray_user_transfers(server_key)
    lines.append(f"parsed_rc={parsed_code}")
    lines.append(f"records={len(records)}")
    if records:
        lines.append("preview:")
        for item in records[:10]:
            lines.append(
                f"- name={item.get('name')}, "
                f"uplink={int(item.get('uplink_bytes_total') or 0)}, "
                f"downlink={int(item.get('downlink_bytes_total') or 0)}"
            )

    return (1 if run_code != 0 or parsed_code != 0 else 0), "\n".join(lines)


def list_users_cached(server_key: str, ttl: float = 3.0) -> Tuple[int, List[str], str]:
    cached = _cache_get(server_key, ttl)
    if cached is not None:
        return cached
    code, names, raw = list_users(server_key)
    _cache_set(server_key, code, names, raw)
    return code, names, raw


def get_uuid_by_name(name: str, server_key: Optional[str] = None) -> Optional[str]:
    local_uuid = get_uuid_local(name)
    if local_uuid:
        return local_uuid
    server_key = server_key or _default_xray_server_key()
    if not server_key:
        return None
    code, _names, raw = list_users_cached(server_key, ttl=3.0)
    if code != 0:
        return None
    lines = raw.strip().splitlines()
    for line in lines[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[0] == name:
            return parts[1]
    return None


def ensure_user(
    name: str,
    server_key: str,
    uuid_value: Optional[str] = None,
    short_id_value: Optional[str] = None,
) -> Tuple[int, str, Optional[str], Optional[str]]:
    uuid_value = uuid_value or get_uuid_local(name) or str(uuid_lib.uuid4())
    short_id = short_id_value or get_short_id_local(name, server_key) or generate_short_id()
    code, out = add_user(name, server_key=server_key, uuid_value=uuid_value, short_id=short_id)
    if code != 0:
        lower_out = (out or "").lower()
        if "already exists" in lower_out or "exists" in lower_out or "duplicate" in lower_out:
            return 0, out, uuid_value, short_id
        return code, out, None, None
    return 0, out, uuid_value, short_id


def delete_user(name: str, server_key: Optional[str] = None) -> Tuple[int, str]:
    try:
        name = validate_profile_name(name)
        if server_key:
            server_key = validate_server_key(server_key)
    except ValueError as exc:
        return 1, str(exc)
    server_key = server_key or _default_xray_server_key()
    if not server_key:
        return 1, "No Xray servers are registered"
    server = get_server(server_key)
    if not server:
        return 1, f"Server {server_key} not found"
    cmd = f"/opt/node-plane-runtime/xray-del-user.sh {shlex.quote(name)}"
    return run_server_command(server, cmd, timeout=120)


def build_vless_link_transport(name: str, uuid: str, transport: str, server_key: str) -> str:
    server = get_server(server_key)
    if not server:
        raise KeyError(server_key)

    ready, reason = get_server_link_status(server_key)
    if not ready:
        raise ValueError(reason)

    short_id = get_short_id_local(name, server_key) or server.xray_short_id or server.xray_sid
    path_prefix = server.xray_xhttp_path_prefix or "/assets"

    if transport == "xhttp":
        path = quote(path_prefix, safe="")
        return (
            f"vless://{uuid}@{server.xray_host}:{server.xray_xhttp_port}"
            f"?encryption=none"
            f"&security=reality"
            f"&sni={server.xray_sni}"
            f"&fp={server.xray_fp}"
            f"&pbk={server.xray_pbk}"
            f"&sid={short_id}"
            f"&type=xhttp"
            f"&path={path}"
            f"#reality-{server.key}-{name}-xhttp"
        )

    return (
        f"vless://{uuid}@{server.xray_host}:{server.xray_tcp_port}"
        f"?encryption=none"
        f"&security=reality"
        f"&sni={server.xray_sni}"
        f"&fp={server.xray_fp}"
        f"&pbk={server.xray_pbk}"
        f"&sid={short_id}"
        f"&type=tcp"
        f"&flow={server.xray_flow}"
        f"#reality-{server.key}-{name}-tcp"
    )


def get_server_link_status(server_key: str) -> tuple[bool, str]:
    server = get_server(server_key)
    if not server:
        return False, f"Server {server_key} not found"

    missing: list[str] = []
    if not server.xray_host:
        missing.append("xray_host")
    if not server.xray_sni:
        missing.append("xray_sni")
    if not server.xray_pbk:
        missing.append("xray_pbk")
    if not server.xray_sid:
        missing.append("xray_sid")

    if missing:
        return False, f"Xray link settings are incomplete for server {server_key}: {', '.join(missing)}"
    return True, "ok"
