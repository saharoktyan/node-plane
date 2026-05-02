from __future__ import annotations

from services.provisioning_state import (
    delete_profile_server_state,
    list_server_provisioning_states,
    upsert_profile_server_state,
)
from services.server_registry import get_server


def reconcile_xray_server_state(server_key: str) -> tuple[int, str]:
    from domain.servers import get_access_methods_for_codes
    from services.node_driver import get_node_driver
    from services.profile_state import profile_store

    try:
        remote_items = get_node_driver().list_remote_profiles(server_key, "xray")
    except Exception as exc:
        return 1, str(exc)

    remote_by_name = {
        str(item.profile_name): {"name": item.profile_name, "uuid": item.remote_id}
        for item in remote_items
        if str(item.profile_name).strip()
    }
    subs = profile_store.read()
    desired_names: set[str] = set()
    ready = 0
    attention = 0
    failed = 0

    for name, rec in subs.items():
        if str(name).startswith("_") or not isinstance(rec, dict):
            continue
        methods = [m for m in get_access_methods_for_codes(rec.get("protocols") or []) if m.protocol_kind == "xray" and m.server_key == server_key]
        if not methods:
            continue
        desired_names.add(str(name))
        uuid_val = rec.get("uuid")
        if not isinstance(uuid_val, str) or not uuid_val.strip():
            upsert_profile_server_state(str(name), server_key, "xray", status="failed", last_error="uuid missing in SQLite")
            failed += 1
            continue

        remote = remote_by_name.get(str(name))
        if not remote:
            upsert_profile_server_state(str(name), server_key, "xray", status="failed", remote_id=uuid_val, last_error="missing on server")
            failed += 1
            continue

        remote_uuid = remote.get("uuid")
        if remote_uuid and str(remote_uuid) != uuid_val:
            upsert_profile_server_state(
                str(name),
                server_key,
                "xray",
                status="needs_attention",
                remote_id=str(remote_uuid),
                last_error=f"uuid mismatch: sqlite={uuid_val} remote={remote_uuid}",
            )
            attention += 1
            continue

        upsert_profile_server_state(str(name), server_key, "xray", status="provisioned", remote_id=uuid_val, last_error=None)
        ready += 1

    existing_rows = list_server_provisioning_states(server_key)
    for row in existing_rows:
        if str(row.get("protocol_kind")) != "xray":
            continue
        profile_name = str(row.get("profile_name") or "")
        if profile_name and profile_name not in desired_names:
            delete_profile_server_state(profile_name, server_key, "xray")

    extra_remote = sorted(name for name in remote_by_name.keys() if name not in desired_names)
    lines = [
        f"server: {server_key}",
        f"ready: {ready}",
        f"attention: {attention}",
        f"failed: {failed}",
        f"remote_only: {len(extra_remote)}",
    ]
    if extra_remote:
        lines.append("remote extra profiles: " + ", ".join(extra_remote[:20]))
    return 0, "\n".join(lines)


def reconcile_awg_server_state(server_key: str) -> tuple[int, str]:
    from domain.servers import get_access_methods_for_codes
    from services.node_driver import get_node_driver
    from services.profile_state import profile_store

    try:
        remote_items = get_node_driver().list_remote_profiles(server_key, "awg")
    except Exception as exc:
        return 1, str(exc)
    remote_names = {str(item.profile_name) for item in remote_items if str(item.profile_name).strip()}
    subs = profile_store.read()
    desired_names: set[str] = set()
    ready = 0
    failed = 0

    for name, rec in subs.items():
        if str(name).startswith("_") or not isinstance(rec, dict):
            continue
        methods = [m for m in get_access_methods_for_codes(rec.get("protocols") or []) if m.protocol_kind == "awg" and m.server_key == server_key]
        if not methods:
            continue
        desired_names.add(str(name))
        if str(name) in remote_names:
            upsert_profile_server_state(str(name), server_key, "awg", status="provisioned", last_error=None)
            ready += 1
        else:
            upsert_profile_server_state(str(name), server_key, "awg", status="failed", last_error="missing in awg config")
            failed += 1

    existing_rows = list_server_provisioning_states(server_key)
    for row in existing_rows:
        if str(row.get("protocol_kind")) != "awg":
            continue
        profile_name = str(row.get("profile_name") or "")
        if profile_name and profile_name not in desired_names:
            delete_profile_server_state(profile_name, server_key, "awg")

    extra_remote = sorted(name for name in remote_names if name not in desired_names)
    lines = [
        f"server: {server_key}",
        f"ready: {ready}",
        f"failed: {failed}",
        f"remote_only: {len(extra_remote)}",
    ]
    if extra_remote:
        lines.append("remote extra profiles: " + ", ".join(extra_remote[:20]))
    return 0, "\n".join(lines)


def reconcile_server_state(server_key: str) -> tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Server {server_key} not found"

    parts: list[str] = []
    overall_code = 0

    if "xray" in server.protocol_kinds:
        code, out = reconcile_xray_server_state(server_key)
        overall_code = max(overall_code, code)
        parts.append("[xray]")
        parts.append(out.strip())

    if "awg" in server.protocol_kinds:
        code, out = reconcile_awg_server_state(server_key)
        overall_code = max(overall_code, code)
        parts.append("[awg]")
        parts.append(out.strip())

    if not parts:
        return 0, f"server: {server_key}\nno managed protocols"
    return overall_code, "\n\n".join(parts)


def reconcile_profile_state(profile_name: str) -> tuple[int, str]:
    from domain.servers import get_access_methods_for_codes
    from services.profile_state import get_profile

    rec = get_profile(profile_name)
    if not rec:
        return 1, f"profile {profile_name} not found"

    server_keys = sorted({m.server_key for m in get_access_methods_for_codes(rec.get("protocols") or [])})
    if not server_keys:
        return 0, f"profile: {profile_name}\nno managed protocols"

    overall = 0
    blocks: list[str] = [f"profile: {profile_name}"]
    for server_key in server_keys:
        code, out = reconcile_server_state(server_key)
        overall = max(overall, code)
        blocks.append(f"[{server_key}]\n{out}")
    return overall, "\n\n".join(blocks)
