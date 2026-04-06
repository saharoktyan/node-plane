from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from db import ensure_schema, get_db
from db.sqlite_db import SQLiteDB
from db.types import DatabaseBackend


TABLE_COLUMNS: list[tuple[str, list[str]]] = [
    ("schema_meta", ["key", "value"]),
    ("profiles", ["name", "created_at", "updated_at"]),
    (
        "profile_state",
        ["profile_name", "access_type", "created_at", "expires_at", "frozen", "warned_before_exp"],
    ),
    ("profile_access_methods", ["profile_name", "access_code"]),
    ("xray_profiles", ["profile_name", "uuid", "enabled", "short_id", "default_transport"]),
    ("xray_transports", ["profile_name", "transport"]),
    (
        "telegram_users",
        [
            "telegram_user_id",
            "chat_id",
            "username",
            "first_name",
            "last_name",
            "profile_name",
            "locale",
            "access_granted",
            "access_request_pending",
            "access_request_sent_at",
            "notify_access_requests",
            "announcement_silent",
            "telemetry_enabled",
            "updated_at",
            "last_key_at",
            "key_issued_count",
        ],
    ),
    (
        "servers",
        [
            "key",
            "region",
            "title",
            "flag",
            "transport",
            "public_host",
            "protocol_kinds",
            "enabled",
            "ssh_host",
            "ssh_port",
            "ssh_user",
            "ssh_key_path",
            "bootstrap_state",
            "notes",
            "xray_config_path",
            "xray_service_name",
            "xray_host",
            "xray_sni",
            "xray_pbk",
            "xray_sid",
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
            "awg_i1_preset",
            "created_at",
            "updated_at",
        ],
    ),
    ("awg_server_configs", ["profile_name", "server_key", "config_text", "wg_conf", "created_at"]),
    (
        "profile_server_state",
        [
            "profile_name",
            "server_key",
            "protocol_kind",
            "desired_enabled",
            "status",
            "remote_id",
            "last_error",
            "created_at",
            "updated_at",
        ],
    ),
    (
        "traffic_samples",
        [
            "profile_name",
            "server_key",
            "protocol_kind",
            "remote_id",
            "rx_bytes_total",
            "tx_bytes_total",
            "sampled_at",
        ],
    ),
]

ALERT_STATE_COLUMNS = [
    "alert_key",
    "server_key",
    "alert_type",
    "severity",
    "payload_json",
    "active",
    "hit_streak",
    "clear_streak",
    "first_seen_at",
    "last_seen_at",
    "last_sent_at",
]

ALERT_STATE_DDL = """
CREATE TABLE IF NOT EXISTS alert_state (
    alert_key TEXT PRIMARY KEY,
    server_key TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    active INTEGER NOT NULL DEFAULT 0,
    hit_streak INTEGER NOT NULL DEFAULT 0,
    clear_streak INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL DEFAULT '',
    last_seen_at TEXT NOT NULL DEFAULT '',
    last_sent_at TEXT NOT NULL DEFAULT ''
)
"""

MIGRATION_MARKERS = {
    "storage_backend": "postgres",
    "sqlite_migration_state": "completed",
}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ensure_postgres_backend(db: DatabaseBackend) -> None:
    if getattr(db, "backend_name", "") != "postgres":
        raise ValueError("SQLite to PostgreSQL migration requires DB_BACKEND=postgres")


def _require_sqlite_source(sqlite_path: str) -> str:
    path = os.path.abspath(str(sqlite_path or "").strip())
    if not path:
        raise ValueError("sqlite_path is required")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"SQLite source not found: {path}")
    return path


def _sqlite_table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _generic_table_exists(conn, name: str) -> bool:
    backend_name = str(getattr(conn, "backend_name", "") or "").lower()
    if backend_name == "postgres":
        try:
            row = conn.execute("SELECT to_regclass(?) AS table_name", (name,)).fetchone()
            return row is not None and row.get("table_name") is not None
        except Exception:
            # Test harnesses may expose the Postgres adapter on top of SQLite.
            pass
    if backend_name == "sqlite":
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        return row is not None
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        return row is not None
    except Exception:
        pass
    try:
        conn.execute(f"SELECT 1 FROM {name} WHERE 1 = 0").fetchall()
        return True
    except Exception:
        return False


def _fetch_rows(conn, table: str, columns: list[str]) -> list[tuple[Any, ...]]:
    selected = ", ".join(columns)
    rows = conn.execute(f"SELECT {selected} FROM {table}").fetchall()
    return [tuple(row[column] for column in columns) for row in rows]


def _insert_rows(conn, table: str, columns: list[str], rows: list[tuple[Any, ...]]) -> None:
    if not rows:
        return
    placeholders = ", ".join("?" for _ in columns)
    selected = ", ".join(columns)
    sql = f"INSERT INTO {table}({selected}) VALUES ({placeholders})"
    for row in rows:
        conn.execute(sql, row)


def _clear_target(conn, include_alert_state: bool) -> None:
    clear_order = [name for name, _cols in TABLE_COLUMNS]
    if include_alert_state or _generic_table_exists(conn, "alert_state"):
        clear_order.append("alert_state")
    for table in reversed(clear_order):
        if _generic_table_exists(conn, table):
            conn.execute(f"DELETE FROM {table}")


def _ensure_alert_state_table(conn) -> None:
    conn.execute(ALERT_STATE_DDL)


def _upsert_schema_meta(conn, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO schema_meta(key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def _counts_for_tables(conn, include_alert_state: bool) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table, _columns in TABLE_COLUMNS:
        row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
        counts[table] = int(row["c"]) if row and row["c"] is not None else 0
    if include_alert_state and _generic_table_exists(conn, "alert_state"):
        row = conn.execute("SELECT COUNT(*) AS c FROM alert_state").fetchone()
        counts["alert_state"] = int(row["c"]) if row and row["c"] is not None else 0
    return counts


def migrate_sqlite_to_backend(sqlite_path: str, dest_db: DatabaseBackend) -> dict[str, Any]:
    _ensure_postgres_backend(dest_db)
    source_path = _require_sqlite_source(sqlite_path)
    source_db = SQLiteDB(source_path)

    with source_db.connect() as source_conn, dest_db.transaction() as dest_conn:
        ensure_schema(source_conn)
        ensure_schema(dest_conn)
        include_alert_state = _sqlite_table_exists(source_conn, "alert_state")
        if include_alert_state:
            _ensure_alert_state_table(dest_conn)

        _clear_target(dest_conn, include_alert_state=include_alert_state)

        for table, columns in TABLE_COLUMNS:
            rows = _fetch_rows(source_conn, table, columns)
            _insert_rows(dest_conn, table, columns, rows)

        if include_alert_state:
            rows = _fetch_rows(source_conn, "alert_state", ALERT_STATE_COLUMNS)
            _insert_rows(dest_conn, "alert_state", ALERT_STATE_COLUMNS, rows)

        _upsert_schema_meta(dest_conn, "storage_backend", "postgres")
        _upsert_schema_meta(dest_conn, "sqlite_migration_state", "completed")
        _upsert_schema_meta(dest_conn, "sqlite_migration_completed_at", _utcnow_iso())
        _upsert_schema_meta(dest_conn, "sqlite_source_path", source_path)

        counts = _counts_for_tables(dest_conn, include_alert_state=include_alert_state)

    return {
        "status": "success",
        "sqlite_path": source_path,
        "counts": counts,
        "included_alert_state": include_alert_state,
    }


def verify_sqlite_to_backend(sqlite_path: str, dest_db: DatabaseBackend) -> dict[str, Any]:
    _ensure_postgres_backend(dest_db)
    source_path = _require_sqlite_source(sqlite_path)
    source_db = SQLiteDB(source_path)

    with source_db.connect() as source_conn, dest_db.connect() as dest_conn:
        ensure_schema(source_conn)
        include_alert_state = _sqlite_table_exists(source_conn, "alert_state")
        source_counts = _counts_for_tables(source_conn, include_alert_state=include_alert_state)
        dest_counts = _counts_for_tables(dest_conn, include_alert_state=include_alert_state)

        for table, count in source_counts.items():
            if table == "schema_meta":
                continue
            if dest_counts.get(table, -1) != count:
                raise ValueError(f"count mismatch for {table}: source={count} dest={dest_counts.get(table, -1)}")
        if not include_alert_state and _generic_table_exists(dest_conn, "alert_state"):
            row = dest_conn.execute("SELECT COUNT(*) AS c FROM alert_state").fetchone()
            alert_count = int(row["c"]) if row and row["c"] is not None else 0
            if alert_count != 0:
                raise ValueError(f"count mismatch for alert_state: source=0 dest={alert_count}")

        source_meta_rows = source_conn.execute("SELECT key, value FROM schema_meta ORDER BY key").fetchall()
        for row in source_meta_rows:
            dest_row = dest_conn.execute("SELECT value FROM schema_meta WHERE key = ?", (row["key"],)).fetchone()
            if not dest_row or str(dest_row["value"]) != str(row["value"]):
                raise ValueError(f"schema_meta mismatch for key={row['key']}")

        for key, expected in MIGRATION_MARKERS.items():
            row = dest_conn.execute("SELECT value FROM schema_meta WHERE key = ?", (key,)).fetchone()
            if not row or str(row["value"]) != expected:
                raise ValueError(f"missing migration marker: {key}")

        completed_at = dest_conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?",
            ("sqlite_migration_completed_at",),
        ).fetchone()
        source_marker = dest_conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?",
            ("sqlite_source_path",),
        ).fetchone()
        if not completed_at or not str(completed_at["value"]).strip():
            raise ValueError("missing migration marker: sqlite_migration_completed_at")
        if not source_marker or str(source_marker["value"]).strip() != source_path:
            raise ValueError("missing migration marker: sqlite_source_path")

        return {
            "status": "success",
            "sqlite_path": source_path,
            "counts": dest_counts,
            "included_alert_state": include_alert_state,
        }


def migrate_sqlite_to_current_backend(sqlite_path: str) -> dict[str, Any]:
    return migrate_sqlite_to_backend(sqlite_path, get_db())


def verify_sqlite_to_current_backend(sqlite_path: str) -> dict[str, Any]:
    return verify_sqlite_to_backend(sqlite_path, get_db())
