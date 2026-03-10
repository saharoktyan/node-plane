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

from handlers import admin_server_wizard
from services import server_bootstrap


class ProbeSummaryTests(unittest.TestCase):
    def test_single_line_note_flattens_multiline_output(self) -> None:
        note = server_bootstrap._single_line_note("line one\nline two\r\nline three\n")
        self.assertEqual(note, "line one | line two | line three")

    def test_format_probe_output_includes_unsupported_bucket(self) -> None:
        body = (
            "PROBE_UNSUPPORTED|local_in_container\n"
            "hostname: local-host\n"
            "пользователь: bot\n"
            "ядро: container\n"
            "reason: Local transport is unavailable while the bot runs inside a container.\n"
            "remediation: Register this node with transport=ssh or run the bot on the host via systemd.\n"
        )
        text = admin_server_wizard._format_probe_output(body, "en")
        self.assertIsNotNone(text)
        self.assertIn("Unsupported in this mode", text)
        self.assertIn("transport=local is unavailable", text)
        self.assertIn("Switch to a supported deployment path", text)

    def test_format_probe_output_localizes_port_summary_lines_for_english(self) -> None:
        body = (
            "hostname: local-host\n"
            "user: bot\n"
            "kernel: linux\n"
            "docker: available\n"
            "tun: available\n"
            "awg_userspace_ready: yes\n"
            "- AWG 51820/udp: свободен, открыт в firewall\n"
        )
        text = admin_server_wizard._format_probe_output(body, "en")
        self.assertIsNotNone(text)
        self.assertIn("AWG 51820/udp: free, firewall open", text)

    def test_format_probe_output_does_not_treat_unavailable_docker_as_ready(self) -> None:
        body = (
            "hostname: local-host\n"
            "user: bot\n"
            "kernel: linux\n"
            "docker: unavailable\n"
            "tun: available\n"
            "awg_userspace_ready: no\n"
        )
        text = admin_server_wizard._format_probe_output(body, "en")
        self.assertIsNotNone(text)
        self.assertNotIn("Docker is available", text)
        self.assertIn("Docker is not ready on the server yet", text)

    def test_format_probe_output_for_bootstrapped_server_does_not_push_bootstrap_again(self) -> None:
        body = (
            "hostname: local-host\n"
            "user: bot\n"
            "kernel: linux\n"
            "docker: available\n"
            "tun: available\n"
            "awg_userspace_ready: yes\n"
        )
        with patch.object(
            admin_server_wizard,
            "get_server",
            return_value=SimpleNamespace(bootstrap_state="bootstrapped"),
        ):
            text = admin_server_wizard._format_probe_output(body, "en", server_key="nl1")
        self.assertIsNotNone(text)
        self.assertNotIn("You can continue to Bootstrap", text)
        self.assertIn("already deployed", text)


if __name__ == "__main__":
    unittest.main()
