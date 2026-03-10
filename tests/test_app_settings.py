from __future__ import annotations

import importlib
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


class AppSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        base = self.tmpdir.name
        os.environ["NODE_PLANE_BASE_DIR"] = base
        os.environ["SQLITE_DB_PATH"] = os.path.join(base, "bot.sqlite3")
        os.environ["SUBS_DB_PATH"] = os.path.join(base, "subs.json")
        os.environ["USERS_DB_PATH"] = os.path.join(base, "users.json")
        os.environ["WG_DB_PATH"] = os.path.join(base, "wg_db.json")

        import config
        import services.app_settings as app_settings
        import services.server_registry as server_registry

        self.config = importlib.reload(config)
        self.app_settings = importlib.reload(app_settings)
        self.server_registry = importlib.reload(server_registry)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_initial_setup_is_shown_when_no_servers_exist(self) -> None:
        self.assertEqual(self.app_settings.get_initial_setup_state(), "pending")
        self.assertTrue(self.app_settings.should_show_initial_admin_setup())

    def test_dismissed_initial_setup_hides_prompt(self) -> None:
        self.app_settings.set_initial_setup_state("dismissed")
        self.assertEqual(self.app_settings.get_initial_setup_state(), "dismissed")
        self.assertFalse(self.app_settings.should_show_initial_admin_setup())

    def test_existing_server_hides_initial_setup_prompt(self) -> None:
        self.server_registry.upsert_server(
            key="ru1",
            title="Russia 1",
            flag="🇷🇺",
            region="russia",
            transport="local",
            protocol_kinds=["xray"],
            public_host="127.0.0.1",
            bootstrap_state="new",
        )
        self.assertTrue(self.app_settings.has_any_servers())
        self.assertFalse(self.app_settings.should_show_initial_admin_setup())


if __name__ == "__main__":
    unittest.main()
