from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
import uuid

TESTS_DIR = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(TESTS_DIR, ".."))
APP_ROOT = os.path.join(REPO_ROOT, "app")
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from db.migrate_sqlite_to_postgres import migrate_sqlite_to_backend, verify_sqlite_to_backend
from db.postgres_db import PostgresDB
from db.schema import ensure_schema
from db.sqlite_db import SQLiteDB


try:
    import psycopg
except ImportError:  # pragma: no cover - optional integration dependency
    psycopg = None


def _db_name_from_dsn(dsn: str) -> str:
    tail = dsn.rsplit("/", 1)[-1]
    return tail.split("?", 1)[0]


def _replace_db_in_dsn(dsn: str, db_name: str) -> str:
    head, _sep, tail = dsn.rpartition("/")
    suffix = ""
    if "?" in tail:
        suffix = "?" + tail.split("?", 1)[1]
    return f"{head}/{db_name}{suffix}"


@unittest.skipUnless(os.getenv("TEST_POSTGRES_DSN"), "TEST_POSTGRES_DSN is required for PostgreSQL integration tests")
@unittest.skipUnless(psycopg is not None and not getattr(psycopg, "__fake__", False), "real psycopg is required for PostgreSQL integration tests")
class SQLiteToRealPostgresMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.sqlite_path = os.path.join(self.tmpdir.name, "source.sqlite3")
        self.sqlite_db = SQLiteDB(self.sqlite_path)
        self.admin_dsn = os.environ["TEST_POSTGRES_DSN"]
        self.base_db_name = _db_name_from_dsn(self.admin_dsn)
        self.test_db_name = f"{self.base_db_name}_np_{uuid.uuid4().hex[:8]}"
        self.test_dsn = _replace_db_in_dsn(self.admin_dsn, self.test_db_name)

        admin_conn = psycopg.connect(self.admin_dsn, autocommit=True)
        try:
            admin_conn.execute(f'DROP DATABASE IF EXISTS "{self.test_db_name}"')
            admin_conn.execute(f'CREATE DATABASE "{self.test_db_name}"')
        finally:
            admin_conn.close()

        with self.sqlite_db.transaction() as conn:
            ensure_schema(conn)
            conn.execute(
                "INSERT INTO profiles(name, created_at, updated_at) VALUES (?, ?, ?)",
                ("alice", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )
            conn.execute(
                "INSERT INTO profile_state(profile_name, access_type, created_at, expires_at, frozen, warned_before_exp) VALUES (?, ?, ?, ?, ?, ?)",
                ("alice", "days", "2026-01-01T00:00:00Z", None, 0, 0),
            )
            conn.execute(
                "INSERT INTO profile_access_methods(profile_name, access_code) VALUES (?, ?)",
                ("alice", "ga"),
            )
            conn.execute(
                """
                INSERT INTO telegram_users(
                    telegram_user_id, chat_id, username, first_name, last_name, profile_name,
                    locale, access_granted, access_request_pending, access_request_sent_at,
                    notify_access_requests, announcement_silent, telemetry_enabled,
                    updated_at, last_key_at, key_issued_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (1001, 2001, "alice", "Alice", "", "alice", "ru", 1, 0, None, 1, 0, 1, "2026-01-01T00:00:00Z", None, 2),
            )
            conn.execute(
                """
                INSERT INTO servers(
                    key, region, title, flag, transport, public_host, protocol_kinds, enabled,
                    ssh_host, ssh_port, ssh_user, ssh_key_path, bootstrap_state, notes,
                    xray_config_path, xray_service_name, xray_host, xray_sni, xray_pbk, xray_sid,
                    xray_short_id, xray_fp, xray_flow, xray_tcp_port, xray_xhttp_port,
                    xray_xhttp_path_prefix, awg_config_path, awg_iface, awg_public_host, awg_port,
                    awg_i1_preset, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "de", "de", "Germany", "DE", "ssh", "de.example.com", "xray", 1,
                    "de.example.com", 22, "root", "/tmp/key", "bootstrapped", "",
                    "/opt/xray/config.json", "xray", "de.example.com", "", "", "",
                    "", "chrome", "xtls-rprx-vision", 443, 8443, "/assets",
                    "/opt/awg/wg0.conf", "wg0", "de.example.com", 51820, "quic",
                    "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
                ),
            )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()
        if psycopg is None:
            return
        admin_conn = psycopg.connect(self.admin_dsn, autocommit=True)
        try:
            admin_conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                (self.test_db_name,),
            )
            admin_conn.execute(f'DROP DATABASE IF EXISTS "{self.test_db_name}"')
        finally:
            admin_conn.close()

    def test_migrate_and_verify_against_real_postgres(self) -> None:
        dest_db = PostgresDB(self.test_dsn)
        result = migrate_sqlite_to_backend(self.sqlite_path, dest_db)
        self.assertEqual(result["status"], "success")
        verify = verify_sqlite_to_backend(self.sqlite_path, dest_db)
        self.assertEqual(verify["status"], "success")
        self.assertEqual(verify["counts"]["profiles"], 1)
        self.assertEqual(verify["counts"]["servers"], 1)

        with dest_db.connect() as conn:
            row = conn.execute("SELECT value FROM schema_meta WHERE key = ?", ("storage_backend",)).fetchone()
            self.assertEqual(str(row["value"]), "postgres")

    def test_migrate_without_alert_state_table_against_real_postgres(self) -> None:
        sqlite_without_alerts = os.path.join(self.tmpdir.name, "source-no-alerts.sqlite3")
        source_db = SQLiteDB(sqlite_without_alerts)
        with source_db.transaction() as conn:
            ensure_schema(conn)
            conn.execute(
                "INSERT INTO profiles(name, created_at, updated_at) VALUES (?, ?, ?)",
                ("bob", "2026-02-01T00:00:00Z", "2026-02-01T00:00:00Z"),
            )

        dest_db = PostgresDB(self.test_dsn)
        result = migrate_sqlite_to_backend(sqlite_without_alerts, dest_db)
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["included_alert_state"])

        verify = verify_sqlite_to_backend(sqlite_without_alerts, dest_db)
        self.assertEqual(verify["status"], "success")
        self.assertEqual(verify["counts"]["profiles"], 1)


if __name__ == "__main__":
    unittest.main()
