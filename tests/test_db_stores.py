from __future__ import annotations

import os
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

from db.schema import ensure_schema
from db.sqlite_db import SQLiteDB
from db.stores import SQLiteAWGStore, SQLiteProfileStateStore, SQLiteTelegramUsersStore


class SQLiteStoresTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = SQLiteDB(f"{self.tmpdir.name}/bot.sqlite3")
        with self.db.transaction() as conn:
            ensure_schema(conn)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_telegram_users_write_upserts_and_removes_stale_rows(self) -> None:
        store = SQLiteTelegramUsersStore(self.db)
        store.write(
            {
                "1": {"username": "u1", "access_granted": True, "locale": "ru"},
                "2": {"username": "u2", "access_granted": False, "locale": "en"},
            }
        )
        store.write(
            {
                "1": {"username": "u1b", "access_granted": False, "locale": "en"},
            }
        )
        rows = store.read()
        self.assertEqual(sorted(rows.keys()), ["1"])
        self.assertEqual(rows["1"]["username"], "u1b")
        self.assertFalse(rows["1"]["access_granted"])
        self.assertEqual(rows["1"]["locale"], "en")

    def test_profile_state_write_replaces_profile_specific_children_only(self) -> None:
        store = SQLiteProfileStateStore(self.db)
        store.write(
            {
                "alice": {
                    "type": "none",
                    "created_at": "2026-01-01T00:00",
                    "updated_at": "2026-01-01T00:00",
                    "protocols": ["gx"],
                    "uuid": "uuid-1",
                    "xray": {"enabled": True, "transports": ["tcp"], "default": "tcp", "short_id": "abcd"},
                },
                "bob": {
                    "type": "none",
                    "created_at": "2026-01-01T00:00",
                    "updated_at": "2026-01-01T00:00",
                    "protocols": ["ga"],
                },
            }
        )
        store.write(
            {
                "alice": {
                    "type": "days",
                    "created_at": "2026-01-01T00:00",
                    "updated_at": "2026-01-02T00:00",
                    "expires_at": "2026-02-01T00:00",
                    "protocols": ["ga"],
                },
            }
        )
        rows = store.read()
        self.assertEqual(sorted(rows.keys()), ["alice"])
        self.assertEqual(rows["alice"]["type"], "days")
        self.assertEqual(rows["alice"]["protocols"], ["ga"])
        self.assertNotIn("uuid", rows["alice"])

    def test_awg_store_write_upserts_and_removes_stale_pairs(self) -> None:
        profile_state = SQLiteProfileStateStore(self.db)
        profile_state.write(
            {
                "alice": {"type": "none", "created_at": "2026-01-01T00:00", "updated_at": "2026-01-01T00:00"},
                "bob": {"type": "none", "created_at": "2026-01-01T00:00", "updated_at": "2026-01-01T00:00"},
            }
        )
        store = SQLiteAWGStore(self.db)
        store.write(
            {
                "alice": {"servers": {"de": {"config": "cfg1", "wg_conf": "wg1", "created_at": "2026-01-01"}}},
                "bob": {"servers": {"lv": {"config": "cfg2", "wg_conf": "wg2", "created_at": "2026-01-01"}}},
            }
        )
        store.write(
            {
                "alice": {"servers": {"de": {"config": "cfg1b", "wg_conf": "wg1b", "created_at": "2026-01-02"}}},
            }
        )
        rows = store.read()
        self.assertEqual(sorted(rows.keys()), ["alice"])
        self.assertEqual(rows["alice"]["servers"]["de"]["config"], "cfg1b")
        self.assertEqual(rows["alice"]["servers"]["de"]["wg_conf"], "wg1b")


if __name__ == "__main__":
    unittest.main()
