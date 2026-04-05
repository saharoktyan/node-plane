from __future__ import annotations

import importlib
import os
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.postgres_test_harness import configure_postgres_test_env

TESTS_DIR = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(TESTS_DIR, ".."))
APP_ROOT = os.path.join(REPO_ROOT, "app")
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


class _FakeMessage:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def reply_text(self, text: str, **kwargs):
        self.calls.append((text, kwargs))


class AdminStartBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        base = self.tmpdir.name
        configure_postgres_test_env(base)
        os.environ["ADMIN_IDS"] = "42"

        import config
        import services.profile_state as profile_state
        import handlers.user_common as user_common

        self.config = importlib.reload(config)
        self.profile_state = importlib.reload(profile_state)
        self.user_common = importlib.reload(user_common)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_start_creates_admin_profile_immediately_on_empty_db(self) -> None:
        message = _FakeMessage()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=42, username="adminuser", first_name="Admin", last_name="User"),
            effective_chat=SimpleNamespace(id=4200),
            effective_message=message,
        )
        context = SimpleNamespace(user_data={})

        with patch.object(self.user_common, "kb_main_menu", return_value="menu"):
            self.user_common.start_cmd(update, context)

        users = self.profile_state.user_store.read()
        self.assertIn("42", users)
        self.assertEqual(users["42"]["profile_name"], "adminuser")
        self.assertTrue(users["42"]["access_granted"])
        self.assertFalse(users["42"]["access_request_pending"])

        profile = self.profile_state.get_profile("adminuser")
        self.assertEqual(profile.get("type"), "none")
        self.assertIsNone(profile.get("expires_at"))
        self.assertFalse(profile.get("frozen", False))
        self.assertEqual(self.profile_state.get_allowed_protocols("adminuser"), [])

        self.assertEqual(self.user_common._resolve_profile_name(42), "adminuser")
        self.assertEqual(len(message.calls), 1)


if __name__ == "__main__":
    unittest.main()
