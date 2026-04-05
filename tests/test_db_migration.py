from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from typing import Iterator

TESTS_DIR = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(TESTS_DIR, ".."))
APP_ROOT = os.path.join(REPO_ROOT, "app")
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from db.migrate_sqlite_to_postgres import migrate_sqlite_to_backend, verify_sqlite_to_backend
from db.schema import ensure_schema
from db.sqlite_db import SQLiteDB


class _FakePostgresConn:
    backend_name = "postgres"

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, query: str, params=None):
        if params is None:
            return self._conn.execute(query)
        return self._conn.execute(query, params)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


class _FakePostgresDB:
    backend_name = "postgres"

    def __init__(self, path: str) -> None:
        self.path = path

    def _open(self) -> _FakePostgresConn:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return _FakePostgresConn(conn)

    @contextmanager
    def connect(self) -> Iterator[_FakePostgresConn]:
        conn = self._open()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[_FakePostgresConn]:
        conn = self._open()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


class SQLiteToPostgresMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.sqlite_path = os.path.join(self.tmpdir.name, "source.sqlite3")
        self.dest_path = os.path.join(self.tmpdir.name, "dest.sqlite3")
        self.source_db = SQLiteDB(self.sqlite_path)
        self.dest_db = _FakePostgresDB(self.dest_path)

        with self.source_db.transaction() as conn:
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
                "INSERT INTO xray_profiles(profile_name, uuid, enabled, short_id, default_transport) VALUES (?, ?, ?, ?, ?)",
                ("alice", "uuid-1", 1, "short-1", "xhttp"),
            )
            conn.execute(
                "INSERT INTO xray_transports(profile_name, transport) VALUES (?, ?)",
                ("alice", "xhttp"),
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
                    "de", "de", "Germany", "DE", "ssh", "de.example.com", "xray,awg", 1,
                    "de.example.com", 22, "root", "/tmp/key", "bootstrapped", "",
                    "/opt/xray/config.json", "xray", "de.example.com", "", "", "",
                    "", "chrome", "xtls-rprx-vision", 443, 8443, "/assets",
                    "/opt/awg/wg0.conf", "wg0", "de.example.com", 51820, "quic",
                    "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
                ),
            )
            conn.execute(
                "INSERT INTO awg_server_configs(profile_name, server_key, config_text, wg_conf, created_at) VALUES (?, ?, ?, ?, ?)",
                ("alice", "de", "vpn://token", "[Interface]\nPrivateKey = key\n\n[Peer]\nPublicKey = peer\n", "2026-01-01T00:00:00Z"),
            )
            conn.execute(
                """
                INSERT INTO profile_server_state(
                    profile_name, server_key, protocol_kind, desired_enabled, status, remote_id, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("alice", "de", "xray", 1, "active", "uuid-1", None, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )
            conn.execute(
                """
                INSERT INTO traffic_samples(
                    profile_name, server_key, protocol_kind, remote_id, rx_bytes_total, tx_bytes_total, sampled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("alice", "de", "xray", "uuid-1", 123, 456, "2026-01-01T00:00:00Z"),
            )
            conn.execute(
                """
                CREATE TABLE alert_state (
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
            )
            conn.execute(
                """
                INSERT INTO alert_state(
                    alert_key, server_key, alert_type, severity, payload_json,
                    active, hit_streak, clear_streak, first_seen_at, last_seen_at, last_sent_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("de:disk_low", "de", "disk_low", "warn", "{}", 1, 2, 0, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", ""),
            )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_migrate_and_verify_sqlite_to_fake_postgres(self) -> None:
        result = migrate_sqlite_to_backend(self.sqlite_path, self.dest_db)
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["included_alert_state"])
        self.assertEqual(result["counts"]["profiles"], 1)
        self.assertEqual(result["counts"]["profile_state"], 1)
        self.assertEqual(result["counts"]["servers"], 1)
        verify = verify_sqlite_to_backend(self.sqlite_path, self.dest_db)
        self.assertEqual(verify["status"], "success")
        self.assertEqual(verify["counts"]["alert_state"], 1)
        self.assertEqual(verify["counts"]["profile_state"], 1)

        with self.dest_db.connect() as conn:
            row = conn.execute("SELECT value FROM schema_meta WHERE key = ?", ("storage_backend",)).fetchone()
            self.assertEqual(str(row["value"]), "postgres")
            state = conn.execute(
                """
                SELECT access_type, expires_at, frozen, warned_before_exp
                FROM profile_state
                WHERE profile_name = ?
                """,
                ("alice",),
            ).fetchone()
            self.assertIsNotNone(state)
            self.assertEqual(str(state["access_type"]), "days")
            self.assertIsNone(state["expires_at"])
            self.assertEqual(int(state["frozen"]), 0)
            self.assertEqual(int(state["warned_before_exp"]), 0)

    def test_migration_is_idempotent_for_target_contents(self) -> None:
        first = migrate_sqlite_to_backend(self.sqlite_path, self.dest_db)
        second = migrate_sqlite_to_backend(self.sqlite_path, self.dest_db)
        self.assertEqual(first["counts"]["traffic_samples"], 1)
        self.assertEqual(second["counts"]["traffic_samples"], 1)
        verify = verify_sqlite_to_backend(self.sqlite_path, self.dest_db)
        self.assertEqual(verify["counts"]["telegram_users"], 1)

    def test_migration_clears_stale_alert_state_when_source_has_no_alerts_table(self) -> None:
        sqlite_without_alerts = os.path.join(self.tmpdir.name, "source-no-alerts.sqlite3")
        source_db = SQLiteDB(sqlite_without_alerts)
        with source_db.transaction() as conn:
            ensure_schema(conn)
            conn.execute(
                "INSERT INTO profiles(name, created_at, updated_at) VALUES (?, ?, ?)",
                ("bob", "2026-02-01T00:00:00Z", "2026-02-01T00:00:00Z"),
            )
        with self.dest_db.transaction() as conn:
            ensure_schema(conn)
            conn.execute(
                """
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
            )
            conn.execute(
                """
                INSERT INTO alert_state(
                    alert_key, server_key, alert_type, severity, payload_json,
                    active, hit_streak, clear_streak, first_seen_at, last_seen_at, last_sent_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("stale", "de", "disk_low", "warn", "{}", 1, 1, 0, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", ""),
            )

        result = migrate_sqlite_to_backend(sqlite_without_alerts, self.dest_db)
        self.assertFalse(result["included_alert_state"])
        verify = verify_sqlite_to_backend(sqlite_without_alerts, self.dest_db)
        self.assertEqual(verify["status"], "success")
        with self.dest_db.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM alert_state").fetchone()
            self.assertEqual(int(row["c"]), 0)


if __name__ == "__main__":
    unittest.main()
