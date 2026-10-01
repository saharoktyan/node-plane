from __future__ import annotations

import argparse

from db import ensure_schema, get_db
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


def cmd_init() -> None:
    db = get_db()
    with db.transaction() as conn:
        ensure_schema(conn)
    print("Database schema initialized: postgres")


def cmd_status() -> None:
    db = get_db()
    with db.transaction() as conn:
        ensure_schema(conn)
    counts = _counts(db)
    print("Database status: postgres")
    for key, value in counts.items():
        print(f"- {key}: {value}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage Node Plane database")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Initialize the configured database schema")
    subparsers.add_parser("status", help="Show current database table counts")
    args = parser.parse_args()
    if args.command == "init":
        cmd_init()
    elif args.command == "status":
        cmd_status()



if __name__ == "__main__":
    main()
