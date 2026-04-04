from __future__ import annotations

import os
import subprocess
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
telegram_error_module = types.ModuleType("telegram.error")
telegram_error_module.BadRequest = Exception
telegram_error_module.RetryAfter = Exception
telegram_ext_module = types.ModuleType("telegram.ext")
telegram_ext_module.CallbackContext = object
sys.modules.setdefault("telegram", telegram_module)
sys.modules.setdefault("telegram.error", telegram_error_module)
sys.modules.setdefault("telegram.ext", telegram_ext_module)

from handlers import admin_server_wizard
from services import server_bootstrap


class ProbeSummaryTests(unittest.TestCase):
    def test_awg_add_script_uses_server_key_prefix_for_display_name(self) -> None:
        script = server_bootstrap.AWG_ADD_SCRIPT
        self.assertIn('SERVER_KEY="${SERVER_KEY:-}"', script)
        self.assertIn('DISPLAY_NAME="$NAME"', script)
        self.assertIn('DISPLAY_NAME="${SERVER_KEY}-${NAME}"', script)
        self.assertIn('"$DISPLAY_NAME" "$CLIENT_PUB" "$CLIENT_PSK" "$FREE_IP" >> "$CFG"', script)
        self.assertIn('"$DISPLAY_NAME"', script)

    def test_render_server_node_env_includes_server_key(self) -> None:
        server = SimpleNamespace(
            key="lv1",
            xray_config_path="/opt/node-plane-runtime/xray/config.json",
            xray_service_name="xray-lv1",
            awg_iface="wg0",
            awg_i1_preset="quic",
            awg_public_host="1.2.3.4",
            public_host="1.2.3.4",
            awg_port=51820,
        )
        content = server_bootstrap.render_server_node_env(server)
        self.assertIn("SERVER_KEY=lv1", content)

    def test_xray_traffic_script_is_valid_bash(self) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".sh", delete=False) as fh:
            fh.write(server_bootstrap.XRAY_TRAFFIC_SCRIPT)
            script_path = fh.name
        self.addCleanup(lambda: os.path.exists(script_path) and os.unlink(script_path))
        result = subprocess.run(
            ["bash", "-n", script_path],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_xray_traffic_script_uses_direct_exec_and_json_parser(self) -> None:
        script = server_bootstrap.XRAY_TRAFFIC_SCRIPT
        self.assertIn('docker_cmd exec -i "$CONTAINER" xray api statsquery', script)
        self.assertNotIn('sh -lc', script)
        self.assertIn("payload = json.loads(text)", script)
        self.assertIn("re.fullmatch(r'user>>>(.*?)>>>traffic>>>(uplink|downlink)'", script)
        self.assertIn("except Exception:", script)

    def test_shell_join_args_quotes_each_argument(self) -> None:
        rendered = server_bootstrap._shell_join_args(
            "/opt/node-plane-runtime/init-xray.sh",
            "/opt/node-plane-runtime/xray/config.json",
            "example.com",
            "www.cloudflare.com",
            443,
            8443,
            '/assets"; touch /tmp/pwned; #',
            "xtls-rprx-vision",
        )
        self.assertIn("'/assets\"; touch /tmp/pwned; #'", rendered)
        self.assertNotIn(' /assets"; touch /tmp/pwned; # ', rendered)

    def test_server_metrics_script_includes_cpu_usage(self) -> None:
        script = server_bootstrap._server_metrics_script()
        self.assertIn('echo "loadavg: $(cut -d\' \' -f1-3 /proc/loadavg)"', script)
        self.assertIn('echo "cpu usage: $cpu_usage"', script)
        self.assertIn('time.sleep(0.2)', script)

    def test_bootstrap_package_scripts_wait_for_apt_locks(self) -> None:
        packages = server_bootstrap._packages_script()
        docker_install = server_bootstrap._install_docker_script()
        for script in (packages, docker_install):
            self.assertIn("apt_wait()", script)
            self.assertIn("fuser /var/lib/dpkg/lock-frontend", script)
            self.assertIn("sleep 5", script)
            self.assertIn('local timeout="${1:-300}"', script)
            self.assertIn("apt_run()", script)

    def test_runtime_files_include_version_metadata(self) -> None:
        files = server_bootstrap._runtime_files()
        self.assertIn("/opt/node-plane-runtime/VERSION", files)
        self.assertIn("/opt/node-plane-runtime/BUILD_COMMIT", files)

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
