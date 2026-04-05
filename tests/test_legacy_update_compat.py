from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO

TESTS_DIR = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(TESTS_DIR, ".."))
APP_ROOT = os.path.join(REPO_ROOT, "app")
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tests.postgres_test_harness import install_fake_psycopg


class LegacyUpdateCompatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base_dir = self.tmpdir.name
        self.shared_dir = os.path.join(self.base_dir, "shared")
        self.release_dir = os.path.join(self.base_dir, "releases", "0.4-test")
        os.makedirs(self.shared_dir, exist_ok=True)
        os.makedirs(self.release_dir, exist_ok=True)
        self.sqlite_path = os.path.join(self.shared_dir, "data", "bot.sqlite3")
        self.postgres_path = os.path.join(self.shared_dir, "data", "bot.pg.sqlite3")
        os.makedirs(os.path.dirname(self.sqlite_path), exist_ok=True)
        with open(os.path.join(self.shared_dir, ".env"), "w", encoding="utf-8") as fh:
            fh.write(f"POSTGRES_DSN={self.postgres_path}\n")
            fh.write(f"SQLITE_DB_PATH={self.sqlite_path}\n")
        install_fake_psycopg()
        os.environ.pop("DB_BACKEND", None)
        os.environ.pop("POSTGRES_DSN", None)
        os.environ.pop("SQLITE_DB_PATH", None)
        os.environ["NODE_PLANE_BASE_DIR"] = self.base_dir
        os.environ["NODE_PLANE_SHARED_DIR"] = self.shared_dir
        os.environ["NODE_PLANE_APP_DIR"] = self.release_dir

        import config
        import db
        import db.schema
        import db.sqlite_db
        import manage_db

        self.config = importlib.reload(config)
        self.db_module = importlib.reload(db)
        self.schema = importlib.reload(db.schema)
        self.sqlite_db = importlib.reload(db.sqlite_db)
        self.manage_db = importlib.reload(manage_db)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_get_db_uses_postgres_dsn_loaded_from_shared_env_file(self) -> None:
        db = self.db_module.get_db()
        self.assertEqual(getattr(db, "backend_name", ""), "postgres")

    def test_init_auto_migrates_legacy_sqlite_into_empty_postgres(self) -> None:
        source_db = self.sqlite_db.SQLiteDB(self.sqlite_path)
        with source_db.transaction() as conn:
            self.schema.ensure_schema(conn)
            conn.execute(
                "INSERT INTO profiles(name, created_at, updated_at) VALUES (?, ?, ?)",
                ("alice", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
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
                (1001, 2001, "alice", "Alice", "", "alice", "ru", 1, 0, None, 1, 0, 0, "2026-01-01T00:00:00Z", None, 1),
            )

        stdout = StringIO()
        with redirect_stdout(stdout):
            self.manage_db.cmd_init()
        self.assertIn("INIT|legacy_sqlite|migrated", stdout.getvalue())

        postgres_db = self.db_module.get_db()
        with postgres_db.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM profiles").fetchone()
            self.assertEqual(int(row["c"]), 1)
            user_row = conn.execute("SELECT profile_name FROM telegram_users WHERE telegram_user_id = ?", (1001,)).fetchone()
            self.assertEqual(str(user_row["profile_name"]), "alice")
            marker = conn.execute("SELECT value FROM schema_meta WHERE key = ?", ("storage_backend",)).fetchone()
            self.assertEqual(str(marker["value"]), "postgres")

    def test_init_skips_legacy_sqlite_import_when_postgres_is_nonempty(self) -> None:
        source_db = self.sqlite_db.SQLiteDB(self.sqlite_path)
        with source_db.transaction() as conn:
            self.schema.ensure_schema(conn)
            conn.execute(
                "INSERT INTO profiles(name, created_at, updated_at) VALUES (?, ?, ?)",
                ("sqlite-user", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )

        postgres_db = self.db_module.get_db()
        with postgres_db.transaction() as conn:
            self.schema.ensure_schema(conn)
            conn.execute(
                "INSERT INTO profiles(name, created_at, updated_at) VALUES (?, ?, ?)",
                ("pg-user", "2026-02-01T00:00:00Z", "2026-02-01T00:00:00Z"),
            )

        stdout = StringIO()
        with redirect_stdout(stdout):
            self.manage_db.cmd_init()
        self.assertIn("INIT|legacy_sqlite|skipped_nonempty_target", stdout.getvalue())

        with postgres_db.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM profiles WHERE name = ?", ("pg-user",)).fetchone()
            self.assertEqual(int(row["c"]), 1)
            sqlite_row = conn.execute("SELECT COUNT(*) AS c FROM profiles WHERE name = ?", ("sqlite-user",)).fetchone()
            self.assertEqual(int(sqlite_row["c"]), 0)

    def test_explicit_migration_skips_nonempty_postgres_target(self) -> None:
        source_db = self.sqlite_db.SQLiteDB(self.sqlite_path)
        with source_db.transaction() as conn:
            self.schema.ensure_schema(conn)
            conn.execute(
                "INSERT INTO profiles(name, created_at, updated_at) VALUES (?, ?, ?)",
                ("sqlite-user", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )

        postgres_db = self.db_module.get_db()
        with postgres_db.transaction() as conn:
            self.schema.ensure_schema(conn)
            conn.execute(
                "INSERT INTO profiles(name, created_at, updated_at) VALUES (?, ?, ?)",
                ("pg-user", "2026-02-01T00:00:00Z", "2026-02-01T00:00:00Z"),
            )

        self.manage_db.cmd_migrate_to_postgres(self.sqlite_path)

        with postgres_db.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM profiles WHERE name = ?", ("pg-user",)).fetchone()
            self.assertEqual(int(row["c"]), 1)

    def test_init_reports_absent_legacy_sqlite_source(self) -> None:
        if os.path.exists(self.sqlite_path):
            os.remove(self.sqlite_path)
        stdout = StringIO()
        with redirect_stdout(stdout):
            self.manage_db.cmd_init()
        self.assertIn("INIT|legacy_sqlite|no_source", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
