from __future__ import annotations

import importlib
import os
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace

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
telegram_ext_module = types.ModuleType("telegram.ext")
telegram_ext_module.CallbackContext = object
sys.modules.setdefault("telegram", telegram_module)
sys.modules.setdefault("telegram.ext", telegram_ext_module)


class AccessGateSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        base = self.tmpdir.name
        configure_postgres_test_env(base)
        os.environ["NODE_PLANE_APP_DIR"] = base
        os.environ["NODE_PLANE_SHARED_DIR"] = base
        os.environ["SQLITE_DB_PATH"] = os.path.join(base, "bot.sqlite3")

        import config
        import services.app_settings as app_settings
        import handlers.user_common as user_common

        self.config = importlib.reload(config)
        self.app_settings = importlib.reload(app_settings)
        self.user_common = importlib.reload(user_common)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_access_gate_uses_custom_phrase_when_requests_disabled(self) -> None:
        self.app_settings.set_access_requests_enabled(False)
        self.app_settings.set_access_gate_message("Authorization needed")
        self.assertEqual(self.user_common._access_gate_text(123, "ru"), "Authorization needed")

    def test_build_start_reply_hides_title_and_request_button_when_requests_disabled(self) -> None:
        self.app_settings.set_access_requests_enabled(False)
        self.app_settings.set_access_gate_message("Authorization needed")
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123, username="u", first_name="A", last_name="B"),
            effective_chat=SimpleNamespace(id=456),
        )
        text, markup = self.user_common._build_start_reply(update, "ru", "2026-04-01T00:00:00Z")
        self.assertEqual(text, "Authorization needed")
        self.assertEqual(markup.inline_keyboard, [])


if __name__ == "__main__":
    unittest.main()
