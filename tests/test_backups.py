from __future__ import annotations

import importlib
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


class BackupsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        base = self.tmpdir.name
        os.environ["NODE_PLANE_BASE_DIR"] = base
        os.environ["NODE_PLANE_APP_DIR"] = base
        os.environ["NODE_PLANE_SHARED_DIR"] = base
        os.environ["DB_BACKEND"] = "postgres"
        os.environ["POSTGRES_DSN"] = "postgresql://unused"

        import config
        import db.schema as schema
        import services.app_settings as app_settings
        import services.backups as backups

        self.config = importlib.reload(config)
        self.schema = importlib.reload(schema)
        self.app_settings = importlib.reload(app_settings)
        self.backups = importlib.reload(backups)
        self.db = _FakePostgresDB(os.path.join(base, "pg.sqlite3"))
        self.app_settings._db = self.db
        self.app_settings._schema_ready = False
        self.backups.get_db = lambda: self.db

        with self.db.transaction() as conn:
            self.schema.ensure_schema(conn)
            conn.execute(
                "INSERT INTO profiles(name, created_at, updated_at) VALUES (?, ?, ?)",
                ("alice", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )
            conn.execute(
                """
                INSERT INTO profile_state(profile_name, access_type, created_at, expires_at, frozen, warned_before_exp)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("alice", "days", "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", 1, 1),
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
                (1, 1, "alice", "Alice", "", "alice", "ru", 1, 0, None, 1, 0, 0, "2026-01-01T00:00:00Z", None, 1),
            )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _count_users(self) -> int:
        with self.db.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM telegram_users").fetchone()
        return int(row["c"])

    def _profile_state_row(self) -> sqlite3.Row | None:
        with self.db.connect() as conn:
            return conn.execute(
                """
                SELECT profile_name, access_type, expires_at, frozen, warned_before_exp
                FROM profile_state
                WHERE profile_name = ?
                """,
                ("alice",),
            ).fetchone()

    def test_create_backup_writes_snapshot_and_metadata(self) -> None:
        result = self.backups.create_backup("manual")
        self.assertEqual(result["status"], "success")
        self.assertTrue(os.path.isfile(result["path"]))
        info = self.backups.get_backup_info(result["name"])
        self.assertIsNotNone(info)
        self.assertEqual(info["trigger"], "manual")
        self.assertEqual(info["backend"], "postgres")
        self.assertTrue(str(info["sha256"]))

    def test_create_backup_skips_duplicate_snapshot(self) -> None:
        first = self.backups.create_backup("manual")
        second = self.backups.create_backup("manual")
        self.assertEqual(first["status"], "success")
        self.assertEqual(second["status"], "skipped_duplicate")
        self.assertEqual(len(self.backups.list_backups()), 1)

    def test_rows_for_table_orders_rows_deterministically(self) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO profiles(name, created_at, updated_at) VALUES (?, ?, ?)",
                ("bob", "2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z"),
            )
            conn.execute(
                """
                INSERT INTO profile_state(profile_name, access_type, created_at, expires_at, frozen, warned_before_exp)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("bob", "none", "2026-01-02T00:00:00Z", None, 0, 0),
            )
        with self.db.connect() as conn:
            rows = self.backups._rows_for_table(
                conn,
                "profile_state",
                ["profile_name", "access_type", "created_at", "expires_at", "frozen", "warned_before_exp"],
            )
        self.assertEqual([row["profile_name"] for row in rows], ["alice", "bob"])

    def test_prune_backups_keeps_latest_count(self) -> None:
        for idx in range(3):
            with self.db.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO telegram_users(
                        telegram_user_id, chat_id, username, first_name, last_name, profile_name,
                        locale, access_granted, access_request_pending, access_request_sent_at,
                        notify_access_requests, announcement_silent, telemetry_enabled,
                        updated_at, last_key_at, key_issued_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (idx + 2, idx + 2, f"user{idx}", "", "", f"user{idx}", "ru", 1, 0, None, 1, 0, 0, "2026-01-01T00:00:00Z", None, 1),
                )
            self.backups.create_backup(f"manual_{idx}")
        result = self.backups.prune_backups(keep_count=2)
        self.assertEqual(len(self.backups.list_backups()), 2)
        self.assertEqual(len(result["removed"]), 1)

    def test_restore_backup_replaces_db_contents(self) -> None:
        backup = self.backups.create_backup("manual")
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM telegram_users")
            conn.execute("DELETE FROM profile_state")
        self.assertEqual(self._count_users(), 0)
        restored = self.backups.restore_backup(backup["name"])
        self.assertEqual(restored["status"], "success")
        self.assertEqual(self._count_users(), 1)
        profile_state = self._profile_state_row()
        self.assertIsNotNone(profile_state)
        self.assertEqual(str(profile_state["access_type"]), "days")
        self.assertEqual(str(profile_state["expires_at"]), "2026-02-01T00:00:00Z")
        self.assertEqual(int(profile_state["frozen"]), 1)
        self.assertEqual(int(profile_state["warned_before_exp"]), 1)

    def test_restore_backup_clears_stale_alert_state_when_backup_has_no_alerts(self) -> None:
        backup = self.backups.create_backup("manual")
        with self.db.transaction() as conn:
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
                ("stale", "spb1", "disk_low", "warn", "{}", 1, 1, 0, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", ""),
            )
        restored = self.backups.restore_backup(backup["name"])
        self.assertEqual(restored["status"], "success")
        with self.db.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM alert_state").fetchone()
            self.assertEqual(int(row["c"]), 0)

    def test_backup_token_resolves_backup_without_long_callback_name(self) -> None:
        backup = self.backups.create_backup("manual")
        token = self.backups.backup_token(backup["name"])
        self.assertLessEqual(len(token), 12)
        resolved = self.backups.resolve_backup_token(token)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["name"], backup["name"])

    def test_scheduled_backup_respects_enabled_and_due_state(self) -> None:
        self.app_settings.set_backups_enabled(True)
        self.app_settings.set_backups_interval_hours(24)
        first = self.backups.run_scheduled_backup_if_due()
        second = self.backups.run_scheduled_backup_if_due()
        self.assertEqual(first["status"], "success")
        self.assertEqual(second["status"], "not_due")

    def test_backups_state_defaults_and_setters(self) -> None:
        self.assertFalse(self.app_settings.is_backups_enabled())
        self.assertEqual(self.app_settings.get_backups_interval_hours(), 24)
        self.assertEqual(self.app_settings.get_backups_keep_count(), 10)
        self.app_settings.set_backups_enabled(True)
        self.app_settings.set_backups_interval_hours(12)
        self.app_settings.set_backups_keep_count(20)
        state = self.app_settings.get_backups_state()
        self.assertTrue(state["enabled"])
        self.assertEqual(state["interval_hours"], 12)
        self.assertEqual(state["keep_count"], 20)


if __name__ == "__main__":
    unittest.main()
