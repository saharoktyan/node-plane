from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from db import ensure_schema, get_db
from services.server_registry import get_server


_db = get_db()
_schema_ready = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="minutes")


def _ensure_runtime_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _db.transaction() as conn:
        ensure_schema(conn)
    _schema_ready = True


def upsert_profile_server_state(
    profile_name: str,
    server_key: str,
    protocol_kind: str,
    *,
    desired_enabled: bool = True,
    status: str,
    remote_id: Optional[str] = None,
    last_error: Optional[str] = None,
) -> None:
    _ensure_runtime_schema()
    now = _now_iso()
    with _db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO profile_server_state(
                profile_name, server_key, protocol_kind, desired_enabled, status,
                remote_id, last_error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(profile_name, server_key, protocol_kind) DO UPDATE SET
                desired_enabled = excluded.desired_enabled,
                status = excluded.status,
                remote_id = excluded.remote_id,
                last_error = excluded.last_error,
                updated_at = excluded.updated_at
            """,
            (
                profile_name,
                server_key,
                protocol_kind,
                1 if desired_enabled else 0,
                status,
                remote_id,
                last_error,
                now,
                now,
            ),
        )


def delete_profile_server_state(profile_name: str, server_key: str, protocol_kind: Optional[str] = None) -> None:
    _ensure_runtime_schema()
    with _db.transaction() as conn:
        if protocol_kind:
            conn.execute(
                """
                DELETE FROM profile_server_state
                WHERE profile_name = ? AND server_key = ? AND protocol_kind = ?
                """,
                (profile_name, server_key, protocol_kind),
            )
        else:
            conn.execute(
                """
                DELETE FROM profile_server_state
                WHERE profile_name = ? AND server_key = ?
                """,
                (profile_name, server_key),
            )


def list_profile_server_states(profile_name: str) -> List[Dict[str, Any]]:
    _ensure_runtime_schema()
    with _db.connect() as conn:
        rows = conn.execute(
            """
            SELECT profile_name, server_key, protocol_kind, desired_enabled, status,
                   remote_id, last_error, created_at, updated_at
            FROM profile_server_state
            WHERE profile_name = ?
            ORDER BY server_key, protocol_kind
            """,
            (profile_name,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_server_provisioning_states(server_key: str) -> List[Dict[str, Any]]:
    _ensure_runtime_schema()
    with _db.connect() as conn:
        rows = conn.execute(
            """
            SELECT profile_name, server_key, protocol_kind, desired_enabled, status,
                   remote_id, last_error, created_at, updated_at
            FROM profile_server_state
            WHERE server_key = ?
            ORDER BY profile_name, protocol_kind
            """,
            (server_key,),
        ).fetchall()
    return [dict(row) for row in rows]


def summarize_server_provisioning(server_key: str) -> Dict[str, Any]:
    rows = list_server_provisioning_states(server_key)
    total = len(rows)
    by_status = {
        "provisioned": 0,
        "needs_attention": 0,
        "failed": 0,
        "pending": 0,
    }
    for row in rows:
        status = str(row.get("status") or "pending")
        by_status[status] = by_status.get(status, 0) + 1

    if by_status["failed"] > 0:
        overall = "failed"
    elif by_status["needs_attention"] > 0:
        overall = "needs_attention"
    elif total > 0 and by_status["provisioned"] == total:
        overall = "provisioned"
    elif total == 0:
        overall = "empty"
    else:
        overall = "pending"

    return {
        "total": total,
        "overall": overall,
        "by_status": by_status,
    }


def render_profile_server_state_summary(profile_name: str, lang: str = "ru") -> str:
    rows = list_profile_server_states(profile_name)
    if not rows:
        return "—"

    def status_icon(value: str) -> str:
        return {
            "provisioned": "•",
            "needs_attention": "!",
            "failed": "×",
            "pending": "…",
        }.get(value, "•")

    def status_label(value: str) -> str:
        if lang == "ru":
            return {
                "provisioned": "готов",
                "needs_attention": "требует внимания",
                "failed": "ошибка",
                "pending": "ожидает",
            }.get(value, value)
        return {
            "provisioned": "ready",
            "needs_attention": "needs attention",
            "failed": "failed",
            "pending": "pending",
        }.get(value, value)

    lines: List[str] = []
    for row in rows:
        server = get_server(str(row["server_key"]))
        server_label = server.title if server else str(row["server_key"])
        server_flag = server.flag if server else "🏳️"
        proto = "Xray" if str(row["protocol_kind"]) == "xray" else "AWG"
        line = f"{status_icon(str(row['status']))} {server_flag} {server_label} / {proto}: {status_label(str(row['status']))}"
        last_error = row.get("last_error")
        if isinstance(last_error, str) and last_error.strip() and str(row["status"]) != "provisioned":
            detail = last_error.strip().splitlines()[0][:120]
            line += f"\n   {detail}"
        lines.append(line)
    return "\n".join(lines)


def render_server_provisioning_summary(server_key: str, lang: str = "ru") -> str:
    summary = summarize_server_provisioning(server_key)
    total = int(summary["total"])
    if total == 0:
        return "—"

    by_status = summary["by_status"]
    if lang == "ru":
        return (
            f"профилей: {total}\n"
            f"• готово: {by_status['provisioned']}\n"
            f"! требует внимания: {by_status['needs_attention']}\n"
            f"× ошибки: {by_status['failed']}\n"
            f"… в ожидании: {by_status['pending']}"
        )
    return (
        f"profiles: {total}\n"
        f"• ready: {by_status['provisioned']}\n"
        f"! attention: {by_status['needs_attention']}\n"
        f"× failed: {by_status['failed']}\n"
        f"… pending: {by_status['pending']}"
    )


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
