from __future__ import annotations

import argparse

from config import SQLITE_DB_PATH
from db.schema import ensure_schema
from db.sqlite_db import SQLiteDB


def _counts(db: SQLiteDB) -> dict[str, int]:
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


def cmd_init() -> None:
    db = SQLiteDB(SQLITE_DB_PATH)
    with db.transaction() as conn:
        ensure_schema(conn)
    print(f"SQLite schema initialized: {SQLITE_DB_PATH}")


def cmd_status() -> None:
    db = SQLiteDB(SQLITE_DB_PATH)
    with db.transaction() as conn:
        ensure_schema(conn)
    counts = _counts(db)
    print(f"SQLite status: {SQLITE_DB_PATH}")
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage Node Plane SQLite database")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Create SQLite schema if it does not exist")
    subparsers.add_parser("status", help="Show current SQLite table counts")
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
    elif args.command == "awg-traffic-debug":
        cmd_awg_traffic_debug(args.server_key)
    elif args.command == "profile-traffic-debug":
        cmd_profile_traffic_debug(args.profile_name, args.protocol_kind)
    elif args.command == "collect-traffic":
        cmd_collect_traffic()


if __name__ == "__main__":
    main()
