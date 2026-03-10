from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

TESTS_DIR = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(TESTS_DIR, ".."))
APP_ROOT = os.path.join(REPO_ROOT, "app")
TMPDIR = tempfile.mkdtemp(prefix="node-plane-test-")
os.environ.setdefault("NODE_PLANE_BASE_DIR", TMPDIR)
os.environ.setdefault("SQLITE_DB_PATH", os.path.join(TMPDIR, "bot.sqlite3"))
os.environ.setdefault("SUBS_DB_PATH", os.path.join(TMPDIR, "subs.json"))
os.environ.setdefault("USERS_DB_PATH", os.path.join(TMPDIR, "users.json"))
os.environ.setdefault("WG_DB_PATH", os.path.join(TMPDIR, "wg_db.json"))
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

telegram_module = types.ModuleType("telegram")
telegram_module.Update = object
telegram_module.InlineKeyboardButton = object
telegram_module.InlineKeyboardMarkup = object
telegram_ext_module = types.ModuleType("telegram.ext")
telegram_ext_module.CallbackContext = object
sys.modules.setdefault("telegram", telegram_module)
sys.modules.setdefault("telegram.ext", telegram_ext_module)

from handlers import user_common


class UserAccessTests(unittest.TestCase):
    def _make_update(self, user_id: int, username: str | None = None):
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id, username=username),
        )

    def test_has_access_requires_access_granted_for_non_admin(self) -> None:
        update = self._make_update(1001, "alice")
        with patch.object(user_common, "ADMIN_IDS", []), patch.object(
            user_common.user_store,
            "read",
            return_value={"1001": {"username": "alice", "access_granted": False, "profile_name": None}},
        ):
            self.assertFalse(user_common._has_access(update))

    def test_resolve_profile_name_uses_explicit_binding_only(self) -> None:
        with patch.object(
            user_common.user_store,
            "read",
            return_value={"1002": {"username": "alice", "profile_name": "bound-profile"}},
        ), patch.object(user_common, "get_profile", side_effect=lambda name: {"ok": True} if name == "bound-profile" else {}):
            self.assertEqual(user_common._resolve_profile_name(1002), "bound-profile")

    def test_resolve_profile_name_does_not_fallback_to_username(self) -> None:
        with patch.object(
            user_common.user_store,
            "read",
            return_value={"1003": {"username": "alice", "profile_name": None}},
        ), patch.object(user_common, "get_profile", return_value={"ok": True}):
            self.assertIsNone(user_common._resolve_profile_name(1003))


if __name__ == "__main__":
    unittest.main()
