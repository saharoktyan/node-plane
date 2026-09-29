from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class HostHealthcheckTests(unittest.TestCase):
    def test_simple_install_uses_shared_env_and_venv_without_compose(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / "scripts"
            scripts.mkdir()
            for name in ("healthcheck.sh", "python_runtime.sh"):
                shutil.copy(ROOT / "scripts" / name, scripts / name)
            (root / "app/telegram_client").mkdir(parents=True)
            for name in ("app/telegram_client/main.py", "requirements.txt", ".env.example"):
                (root / name).touch()
            shared = root / "shared"
            shared.mkdir()
            (shared / "data").mkdir()
            (shared / "ssh").mkdir()
            (shared / "data/telegram-adapter.token").write_text("test-token\n")
            release = root / "release"
            (release / ".venv/bin").mkdir(parents=True)
            current = root / "current"
            current.symlink_to(release, target_is_directory=True)
            (shared / ".env").write_text(
                f"BOT_TOKEN=test\nADMIN_IDS=42\nNODE_PLANE_BASE_DIR={root}\n"
                f"NODE_PLANE_APP_DIR={current}\nNODE_PLANE_SHARED_DIR={shared}\n"
                "DB_BACKEND=postgres\nPOSTGRES_DSN=postgresql://test\n"
                f"NODE_PLANE_BACKEND_ADAPTER_TOKEN_FILE={shared / 'data/telegram-adapter.token'}\n"
            )
            bins = root / "bin"
            bins.mkdir()
            commands = {
                "systemctl": 'case "$1" in show) echo loaded;; is-active) exit 0;; esac',
                "docker": 'case "$1" in info) exit 0;; *) exit 1;; esac',
            }
            for name, body in commands.items():
                command = bins / name
                command.write_text("#!/bin/sh\n" + body + "\n")
                command.chmod(0o755)
            python = release / ".venv/bin/python"
            python.write_text("#!/bin/sh\nexit 0\n")
            python.chmod(0o755)
            env = dict(os.environ, NODE_PLANE_SHARED_DIR=str(shared), PATH=f"{bins}:{os.environ['PATH']}")
            result = subprocess.run(["bash", str(scripts / "healthcheck.sh")], env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Mode: simple", result.stdout)
            self.assertIn("PostgreSQL runtime is configured", result.stdout)
            self.assertNotIn("Compose", result.stdout)
            self.assertNotIn("[FAIL]", result.stdout)

    def test_missing_unit_is_not_reported_as_installed(self):
        script = (ROOT / "scripts/healthcheck.sh").read_text()
        start = script.index("systemd_unit_installed() {")
        end = script.index("\ndetect_mode()", start)
        command = 'has_cmd() { return 0; }; systemctl() { echo not-found; };\n' + script[start:end] + '\nsystemd_unit_installed'
        result = subprocess.run(["bash", "-c", command], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
