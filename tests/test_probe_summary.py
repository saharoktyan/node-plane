from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.postgres_test_harness import configure_postgres_test_env

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = REPO_ROOT / "app"
configure_postgres_test_env(tempfile.mkdtemp(prefix="node-plane-test-"))
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

telegram_module = types.ModuleType("telegram")
telegram_module.Update = object
telegram_module.InlineKeyboardButton = object
telegram_module.InlineKeyboardMarkup = object
telegram_error_module = types.ModuleType("telegram.error")
telegram_error_module.BadRequest = Exception
telegram_error_module.RetryAfter = Exception
telegram_ext_module = types.ModuleType("telegram.ext")
telegram_ext_module.CallbackContext = object
sys.modules.setdefault("telegram", telegram_module)
sys.modules.setdefault("telegram.error", telegram_error_module)
sys.modules.setdefault("telegram.ext", telegram_ext_module)

from handlers import admin_server_wizard


class ProbeSummaryTests(unittest.TestCase):
    def test_awg_add_script_uses_server_key_prefix_for_display_name(self) -> None:
        script = (REPO_ROOT / "runtime_assets" / "awg-add-user.sh").read_text(encoding="utf-8")
        self.assertIn('SERVER_KEY="${SERVER_KEY:-}"', script)
        self.assertIn('DISPLAY_NAME="${SERVER_KEY}-${NAME}"', script)

    def test_xray_traffic_script_is_valid_bash(self) -> None:
        script_path = REPO_ROOT / "runtime_assets" / "xray-list-traffic.sh"
        result = subprocess.run(["bash", "-n", str(script_path)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

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

    def test_format_probe_output_localizes_port_summary_lines_for_english(self) -> None:
        body = (
            "hostname: local-host\nuser: bot\nkernel: linux\n"
            "docker: available\ntun: available\nawg_userspace_ready: yes\n"
            "- AWG 51820/udp: свободен, открыт в firewall\n"
        )
        text = admin_server_wizard._format_probe_output(body, "en")
        self.assertIsNotNone(text)
        self.assertIn("AWG 51820/udp: free, firewall open", text)

    def test_format_probe_output_does_not_treat_unavailable_docker_as_ready(self) -> None:
        body = "hostname: local-host\nuser: bot\nkernel: linux\ndocker: unavailable\ntun: available\nawg_userspace_ready: no\n"
        text = admin_server_wizard._format_probe_output(body, "en")
        self.assertIsNotNone(text)
        self.assertIn("Docker is not ready on the server yet", text)

    def test_format_probe_output_for_bootstrapped_server_does_not_push_bootstrap_again(self) -> None:
        body = "hostname: local-host\nuser: bot\nkernel: linux\ndocker: available\ntun: available\nawg_userspace_ready: yes\n"
        with patch.object(admin_server_wizard, "get_server", return_value=SimpleNamespace(bootstrap_state="bootstrapped")):
            text = admin_server_wizard._format_probe_output(body, "en", server_key="nl1")
        self.assertIsNotNone(text)
        self.assertNotIn("You can continue to Bootstrap", text)
        self.assertIn("already deployed", text)


if __name__ == "__main__":
    unittest.main()
