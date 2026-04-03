from __future__ import annotations

import importlib
import os
import sqlite3
import sys
import tempfile
import unittest

TESTS_DIR = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(TESTS_DIR, ".."))
APP_ROOT = os.path.join(REPO_ROOT, "app")
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


class BackupsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        base = self.tmpdir.name
        os.environ["NODE_PLANE_BASE_DIR"] = base
        os.environ["NODE_PLANE_APP_DIR"] = base
        os.environ["NODE_PLANE_SHARED_DIR"] = base
        os.environ["SQLITE_DB_PATH"] = os.path.join(base, "data", "bot.sqlite3")

        import config
        import db.schema as schema
        import services.app_settings as app_settings
        import services.backups as backups

        self.config = importlib.reload(config)
        self.schema = importlib.reload(schema)
        self.app_settings = importlib.reload(app_settings)
        self.backups = importlib.reload(backups)

        os.makedirs(os.path.dirname(self.config.SQLITE_DB_PATH), exist_ok=True)
        conn = sqlite3.connect(self.config.SQLITE_DB_PATH)
        try:
            conn.row_factory = sqlite3.Row
            self.schema.ensure_schema(conn)
            conn.execute(
                "INSERT INTO telegram_users(telegram_user_id, chat_id, access_granted) VALUES (?, ?, ?)",
                (1, 1, 1),
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _count_users(self) -> int:
        conn = sqlite3.connect(self.config.SQLITE_DB_PATH)
        try:
            row = conn.execute("SELECT COUNT(*) FROM telegram_users").fetchone()
            return int(row[0])
        finally:
            conn.close()

    def test_create_backup_writes_snapshot_and_metadata(self) -> None:
        result = self.backups.create_backup("manual")
        self.assertEqual(result["status"], "success")
        self.assertTrue(os.path.isfile(result["path"]))
        info = self.backups.get_backup_info(result["name"])
        self.assertIsNotNone(info)
        self.assertEqual(info["trigger"], "manual")
        self.assertTrue(str(info["sha256"]))

    def test_create_backup_skips_duplicate_snapshot(self) -> None:
        first = self.backups.create_backup("manual")
        second = self.backups.create_backup("manual")
        self.assertEqual(first["status"], "success")
        self.assertEqual(second["status"], "skipped_duplicate")
        self.assertEqual(len(self.backups.list_backups()), 1)

    def test_prune_backups_keeps_latest_count(self) -> None:
        for idx in range(3):
            conn = sqlite3.connect(self.config.SQLITE_DB_PATH)
            try:
                conn.execute(
                    "INSERT INTO telegram_users(telegram_user_id, chat_id, access_granted) VALUES (?, ?, ?)",
                    (idx + 2, idx + 2, 1),
                )
                conn.commit()
            finally:
                conn.close()
            self.backups.create_backup(f"manual_{idx}")
        result = self.backups.prune_backups(keep_count=2)
        self.assertEqual(len(self.backups.list_backups()), 2)
        self.assertEqual(len(result["removed"]), 1)

    def test_restore_backup_replaces_db_contents(self) -> None:
        backup = self.backups.create_backup("manual")
        conn = sqlite3.connect(self.config.SQLITE_DB_PATH)
        try:
            conn.execute("DELETE FROM telegram_users")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self._count_users(), 0)
        restored = self.backups.restore_backup(backup["name"])
        self.assertEqual(restored["status"], "success")
        self.assertEqual(self._count_users(), 1)

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
