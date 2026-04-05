from __future__ import annotations

import argparse
import os

from config import DB_BACKEND, SQLITE_DB_PATH
from db import ensure_schema, get_db
from db.migrate_sqlite_to_postgres import migrate_sqlite_to_current_backend, verify_sqlite_to_current_backend
from db.types import DatabaseBackend


def _counts(db: DatabaseBackend) -> dict[str, int]:
    with db.connect() as conn:
        return {
            "servers": int(conn.execute("SELECT COUNT(*) AS c FROM servers").fetchone()["c"]),
            "profiles": int(conn.execute("SELECT COUNT(*) AS c FROM profiles").fetchone()["c"]),
            "profile_state": int(conn.execute("SELECT COUNT(*) AS c FROM profile_state").fetchone()["c"]),
            "access_methods": int(conn.execute("SELECT COUNT(*) AS c FROM profile_access_methods").fetchone()["c"]),
            "xray_profiles": int(conn.execute("SELECT COUNT(*) AS c FROM xray_profiles").fetchone()["c"]),
            "xray_transports": int(conn.execute("SELECT COUNT(*) AS c FROM xray_transports").fetchone()["c"]),
            "awg_configs": int(conn.execute("SELECT COUNT(*) AS c FROM awg_server_configs").fetchone()["c"]),
            "telegram_users": int(conn.execute("SELECT COUNT(*) AS c FROM telegram_users").fetchone()["c"]),
        }


def _table_exists(conn, table: str) -> bool:
    try:
        conn.execute(f"SELECT 1 FROM {table} WHERE 1 = 0").fetchall()
        return True
    except Exception:
        return False


def _runtime_db_is_empty(db: DatabaseBackend) -> bool:
    counts = _counts(db)
    if any(int(value) > 0 for value in counts.values()):
        return False
    with db.connect() as conn:
        if _table_exists(conn, "alert_state"):
            row = conn.execute("SELECT COUNT(*) AS c FROM alert_state").fetchone()
            if row and int(row["c"]) > 0:
                return False
    return True


def _maybe_prepare_legacy_sqlite_migration(db: DatabaseBackend) -> str:
    if DB_BACKEND != "postgres":
        print("INIT|legacy_sqlite|unsupported_backend")
        return "unsupported_backend"
    sqlite_path = str(SQLITE_DB_PATH or "").strip()
    if not sqlite_path or not os.path.isfile(sqlite_path):
        print("INIT|legacy_sqlite|no_source")
        return "no_source"
    if not _runtime_db_is_empty(db):
        print("INIT|legacy_sqlite|skipped_nonempty_target")
        print(f"sqlite_path: {sqlite_path}")
        print(f"Legacy SQLite import skipped because PostgreSQL already contains runtime data: {sqlite_path}")
        return "skipped_nonempty_target"
    try:
        verify_sqlite_to_current_backend(sqlite_path)
        print("INIT|legacy_sqlite|up_to_date")
        print(f"sqlite_path: {sqlite_path}")
        print(f"Legacy SQLite source already matches PostgreSQL: {sqlite_path}")
        return "up_to_date"
    except Exception:
        pass
    result = migrate_sqlite_to_current_backend(sqlite_path)
    print("INIT|legacy_sqlite|migrated")
    print(f"sqlite_path: {result['sqlite_path']}")
    print(f"Legacy SQLite migrated to PostgreSQL: {result['sqlite_path']}")
    verify_sqlite_to_current_backend(sqlite_path)
    print("Legacy SQLite migration verified.")
    return "migrated"


def cmd_init() -> None:
    db = get_db()
    with db.transaction() as conn:
        ensure_schema(conn)
    print("Database schema initialized: postgres")
    _maybe_prepare_legacy_sqlite_migration(db)


def cmd_status() -> None:
    db = get_db()
    with db.transaction() as conn:
        ensure_schema(conn)
    counts = _counts(db)
    print("Database status: postgres")
    for key, value in counts.items():
        print(f"- {key}: {value}")


def cmd_awg_traffic_debug(server_key: str) -> None:
    from services.traffic_usage import debug_awg_traffic_report

    code, out = debug_awg_traffic_report(server_key)
    if code != 0:
        raise SystemExit(out)
    print(out)


def cmd_profile_traffic_debug(profile_name: str, protocol_kind: str) -> None:
    from services.traffic_usage import debug_profile_traffic_report

    code, out = debug_profile_traffic_report(profile_name, protocol_kind)
    if code != 0:
        raise SystemExit(out)
    print(out)


def cmd_collect_traffic() -> None:
    from services.traffic_usage import run_collect_traffic_once

    code, out = run_collect_traffic_once()
    if code != 0:
        raise SystemExit(out)
    print(out)


def cmd_migrate_to_postgres(sqlite_path: str) -> None:
    db = get_db()
    with db.transaction() as conn:
        ensure_schema(conn)
    if not _runtime_db_is_empty(db):
        print("MIGRATE|skipped_nonempty_target")
        print(f"backend: {DB_BACKEND}")
        print(f"sqlite_path: {sqlite_path}")
        print("message: target PostgreSQL already contains runtime data; legacy SQLite import was skipped")
        return
    result = migrate_sqlite_to_current_backend(sqlite_path)
    print("MIGRATE|success")
    print(f"backend: {DB_BACKEND}")
    print(f"sqlite_path: {result['sqlite_path']}")
    print(f"included_alert_state: {'yes' if result['included_alert_state'] else 'no'}")
    for key, value in sorted(result["counts"].items()):
        print(f"{key}: {value}")


def cmd_verify_migration(sqlite_path: str) -> None:
    result = verify_sqlite_to_current_backend(sqlite_path)
    print("VERIFY|success")
    print(f"backend: {DB_BACKEND}")
    print(f"sqlite_path: {result['sqlite_path']}")
    print(f"included_alert_state: {'yes' if result['included_alert_state'] else 'no'}")
    for key, value in sorted(result["counts"].items()):
        print(f"{key}: {value}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage Node Plane database")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Initialize the configured database schema")
    subparsers.add_parser("status", help="Show current database table counts")
    migrate_parser = subparsers.add_parser("migrate-to-postgres", help="Migrate the current SQLite database into PostgreSQL")
    migrate_parser.add_argument("--sqlite-path", default=SQLITE_DB_PATH, help="Path to the source SQLite database")
    verify_parser = subparsers.add_parser("verify-migration", help="Verify that PostgreSQL matches the source SQLite database")
    verify_parser.add_argument("--sqlite-path", default=SQLITE_DB_PATH, help="Path to the source SQLite database")
    awg_debug_parser = subparsers.add_parser("awg-traffic-debug", help="Debug AWG peer matching and traffic sampling for a server")
    awg_debug_parser.add_argument("server_key", help="Registered server key")
    profile_debug_parser = subparsers.add_parser("profile-traffic-debug", help="Debug stored traffic samples and deltas for a profile")
    profile_debug_parser.add_argument("profile_name", help="Profile name")
    profile_debug_parser.add_argument("protocol_kind", choices=["awg", "xray"], help="Protocol kind")
    subparsers.add_parser("collect-traffic", help="Run one traffic collection cycle immediately")

    args = parser.parse_args()
    if args.command == "init":
        cmd_init()
    elif args.command == "status":
        cmd_status()
    elif args.command == "migrate-to-postgres":
        cmd_migrate_to_postgres(args.sqlite_path)
    elif args.command == "verify-migration":
        cmd_verify_migration(args.sqlite_path)
    elif args.command == "awg-traffic-debug":
        cmd_awg_traffic_debug(args.server_key)
    elif args.command == "profile-traffic-debug":
        cmd_profile_traffic_debug(args.profile_name, args.protocol_kind)
    elif args.command == "collect-traffic":
        cmd_collect_traffic()


if __name__ == "__main__":
    main()
