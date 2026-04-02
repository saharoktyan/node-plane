from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

TESTS_DIR = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(TESTS_DIR, ".."))
APP_ROOT = os.path.join(REPO_ROOT, "app")
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


class UpdatesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        base = self.tmpdir.name
        os.environ["NODE_PLANE_BASE_DIR"] = base
        os.environ["NODE_PLANE_APP_DIR"] = base
        os.environ["NODE_PLANE_SHARED_DIR"] = base
        os.environ["NODE_PLANE_SOURCE_DIR"] = "/opt/node-plane-src"
        os.environ["NODE_PLANE_INSTALL_MODE"] = "simple"
        os.environ["SQLITE_DB_PATH"] = os.path.join(base, "bot.sqlite3")
        os.makedirs(os.path.join(base, "scripts"), exist_ok=True)
        with open(os.path.join(base, "scripts", "update.sh"), "w", encoding="utf-8") as fh:
            fh.write("#!/usr/bin/env bash\n")
        with open(os.path.join(base, "scripts", "check_updates.sh"), "w", encoding="utf-8") as fh:
            fh.write("#!/usr/bin/env bash\n")

        import config
        import services.app_settings as app_settings
        import services.updates as updates

        self.config = importlib.reload(config)
        self.app_settings = importlib.reload(app_settings)
        self.updates = importlib.reload(updates)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_check_for_updates_records_available_state(self) -> None:
        proc = SimpleNamespace(
            returncode=0,
            stdout=(
                "CHECK_UPDATES|available\n"
                "branch: dev\n"
                "upstream_ref: origin/main\n"
                "local_version: 0.1.0\n"
                "remote_version: 0.1.1\n"
                "local_label: 0.1.0 · abc1234\n"
                "remote_label: 0.1.1 · def5678\n"
            ),
            stderr="",
        )
        with patch("services.updates.subprocess.run", return_value=proc):
            result = self.updates.check_for_updates()
        self.assertEqual(result["status"], "available")
        overview = self.updates.get_updates_overview()
        self.assertTrue(overview["update_available"])
        self.assertEqual(overview["remote_label"], "0.1.1 · def5678")
        self.assertEqual(overview["upstream_ref"], "origin/main")
        self.assertEqual(overview["branch"], "dev")

    def test_check_for_updates_records_error_state(self) -> None:
        proc = SimpleNamespace(
            returncode=1,
            stdout="CHECK_UPDATES|error\nmessage: git fetch failed\n",
            stderr="",
        )
        with patch("services.updates.subprocess.run", return_value=proc):
            result = self.updates.check_for_updates()
        self.assertEqual(result["status"], "error")
        overview = self.updates.get_updates_overview()
        self.assertFalse(overview["update_available"])
        self.assertEqual(overview["last_error"], "git fetch failed")

    def test_overview_clears_stale_available_when_versions_match(self) -> None:
        self.app_settings.record_update_check(
            {
                "checked_at": "2026-04-01T00:00:00Z",
                "status": "available",
                "local_label": self.config.APP_VERSION,
                "remote_label": self.config.APP_VERSION,
                "upstream_ref": "origin/main",
            }
        )
        overview = self.updates.get_updates_overview()
        self.assertFalse(overview["update_available"])
        self.assertEqual(overview["last_status"], "up_to_date")

    def test_schedule_update_records_running_state(self) -> None:
        proc = SimpleNamespace(returncode=0, stdout="Running as unit node-plane-update-1.service.\n", stderr="")
        with patch("services.updates.subprocess.run", return_value=proc):
            result = self.updates.schedule_update(branch="dev", target_ref="v0.2.0-alpha.1")
        self.assertEqual(result["status"], "running")
        state = self.app_settings.get_update_state()
        self.assertEqual(state["last_run_status"], "running")
        self.assertTrue(str(state["last_run_unit"]).startswith("node-plane-update-"))

    def test_list_available_versions_parses_tags_and_actions(self) -> None:
        proc = SimpleNamespace(
            returncode=0,
            stdout=(
                "LIST_VERSIONS|ok\n"
                "branch: dev\n"
                "current_version: 0.2.0-alpha.2\n"
                "version_item: 0.2.0-alpha.3|v0.2.0-alpha.3|tag\n"
                "version_item: 0.2.0-alpha.2|v0.2.0-alpha.2|tag\n"
                "version_item: 0.2.0-alpha.1|v0.2.0-alpha.1|tag\n"
                "version_item: 0.1.5|v0.1.5|tag\n"
            ),
            stderr="",
        )
        with patch("services.updates.subprocess.run", return_value=proc), patch.object(self.updates, "APP_SEMVER", "0.2.0-alpha.2"):
            result = self.updates.list_available_versions(branch="dev")
        actions = {item["version"]: item["action"] for item in result["versions"]}
        self.assertEqual(actions["0.2.0-alpha.3"], "upgrade")
        self.assertEqual(actions["0.2.0-alpha.2"], "current")
        self.assertEqual(actions["0.2.0-alpha.1"], "downgrade")
        self.assertEqual(actions["0.1.5"], "blocked")

    def test_get_version_transition_allows_major_upgrade_and_blocks_major_downgrade(self) -> None:
        major_upgrade = self.updates.get_version_transition("0.7.1", "1.0.3")
        self.assertTrue(major_upgrade["allowed"])
        self.assertEqual(major_upgrade["action"], "upgrade")
        self.assertEqual(major_upgrade["reason"], "major_upgrade")

        major_downgrade = self.updates.get_version_transition("1.0.3", "0.7.1")
        self.assertFalse(major_downgrade["allowed"])
        self.assertEqual(major_downgrade["action"], "blocked")
        self.assertEqual(major_downgrade["reason"], "major_downgrade_blocked")

    def test_get_version_transition_blocks_pre1_minor_downgrade(self) -> None:
        blocked = self.updates.get_version_transition("0.2.0", "0.1.5")
        self.assertFalse(blocked["allowed"])
        self.assertEqual(blocked["reason"], "pre1_minor_downgrade_blocked")

    def test_refresh_update_run_state_records_success(self) -> None:
        self.app_settings.record_update_run_started("2026-04-01T00:00:00Z", "node-plane-update-1")
        show_proc = SimpleNamespace(
            returncode=0,
            stdout="LoadState=loaded\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\n",
            stderr="",
        )
        journal_proc = SimpleNamespace(returncode=0, stdout="update complete\n", stderr="")
        with patch("services.updates.subprocess.run", side_effect=[show_proc, journal_proc]):
            state = self.updates.refresh_update_run_state()
        self.assertEqual(state["last_run_status"], "success")
        self.assertEqual(state["last_run_log_tail"], "")

    def test_auto_check_job_skips_when_disabled(self) -> None:
        self.app_settings.set_updates_auto_check_enabled(False)
        with patch("services.updates.check_for_updates") as mocked:
            self.updates.auto_check_job()
        mocked.assert_not_called()

    def test_auto_check_job_runs_when_enabled(self) -> None:
        self.app_settings.set_updates_auto_check_enabled(True)
        with patch("services.updates.check_for_updates", return_value={"status": "up_to_date"}) as mocked:
            self.updates.auto_check_job()
        mocked.assert_called_once()

    def test_menu_emoji_is_neutral_when_auto_check_disabled_and_no_known_update(self) -> None:
        self.app_settings.set_updates_auto_check_enabled(False)
        emoji = self.updates.get_updates_menu_emoji(
            {
                "auto_check_enabled": False,
                "last_run_status": "never",
                "last_status": "never",
                "update_available": False,
            }
        )
        self.assertEqual(emoji, "📦")

    def test_menu_emoji_keeps_new_when_update_is_known_while_auto_check_disabled(self) -> None:
        self.app_settings.set_updates_auto_check_enabled(False)
        emoji = self.updates.get_updates_menu_emoji(
            {
                "auto_check_enabled": False,
                "last_run_status": "never",
                "last_status": "available",
                "update_available": True,
            }
        )
        self.assertEqual(emoji, "🆕")

    def test_updates_branch_defaults_from_env(self) -> None:
        os.environ["NODE_PLANE_UPDATE_BRANCH"] = "dev"
        import config
        import services.app_settings as app_settings
        self.config = importlib.reload(config)
        self.app_settings = importlib.reload(app_settings)
        self.assertEqual(self.app_settings.get_updates_branch(), "dev")


if __name__ == "__main__":
    unittest.main()
