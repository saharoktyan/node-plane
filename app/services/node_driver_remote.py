from __future__ import annotations

from typing import Any

from services.server_registry import get_server
from services.server_runtime import run_server_command
from services.xray import list_user_records


def parse_awg_profile_names(config_text: str) -> set[str]:
    names: set[str] = set()
    for raw_line in config_text.splitlines():
        line = raw_line.strip()
        if not line.startswith("#"):
            continue
        name = line.lstrip("#").strip()
        if name:
            names.add(name)
    return names


def list_remote_xray_profiles(server_key: str) -> tuple[int, list[dict[str, Any]], str]:
    code, records, raw = list_user_records(server_key)
    if code != 0:
        return code, [], raw
    items = [dict(record) for record in records if record.get("name")]
    return 0, items, raw


def list_remote_awg_profiles(server_key: str) -> tuple[int, set[str], str]:
    server = get_server(server_key)
    if not server:
        return 1, set(), f"Server {server_key} not found"

    code, raw = run_server_command(server, f"cat {server.awg_config_path}", timeout=60)
    if code != 0:
        return code, set(), raw
    return 0, parse_awg_profile_names(raw), raw
