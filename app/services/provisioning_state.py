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
