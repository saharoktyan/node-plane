from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class HostHealthcheckTests(unittest.TestCase):
    def _create_layout(self, directory: str | Path):
        root = Path(directory) / "node plane path with spaces"
        releases = root / "releases"
        release = releases / "v1.2.3"
        (release / "scripts").mkdir(parents=True)
        shutil.copy(ROOT / "scripts" / "healthcheck.sh", release / "scripts" / "healthcheck.sh")
        shutil.copy(ROOT / "scripts" / "python_runtime.sh", release / "scripts" / "python_runtime.sh")
        (release / "app" / "telegram_client").mkdir(parents=True)
        (release / "app" / "backend").mkdir(parents=True)
        (release / "app" / "telegram_client" / "main.py").touch()
        (release / "app" / "backend" / "http_api.py").touch()
        (release / "pyproject.toml").touch()
        (release / "uv.lock").touch()
        (release / ".env.example").touch()

        shared = root / "shared"
        (shared / "data").mkdir(parents=True)
        (shared / "ssh").mkdir(parents=True)
        adapter_token_file = shared / "data" / "telegram-adapter.token"
        adapter_token_file.write_text("adapter-secret-token\n")

        current = root / "current"
        current.symlink_to(release, target_is_directory=True)

        canary = root / "canary_execution.txt"
        env_content = (
            "BOT_TOKEN=secret_bot_token_98765\n"
            "ADMIN_IDS=424242\n"
            "DB_BACKEND=postgres\n"
            "POSTGRES_DSN=postgresql://secret_user:secret_pass_12345@127.0.0.1:5432/db\n"
            f"NODE_PLANE_BACKEND_ADAPTER_TOKEN_FILE={adapter_token_file}\n"
            f"MARKER_SUBST=$(touch '{canary}')\n"
        )
        (shared / ".env").write_text(env_content)

        bins = root / "bin"
        bins.mkdir(parents=True)
        commands = {
            "systemctl": 'case "$1" in show) echo loaded;; is-active) exit 0;; *) exit 0;; esac',
            "docker": 'case "$1" in info) exit 0;; *) exit 1;; esac',
        }
        for name, body in commands.items():
            cmd = bins / name
            cmd.write_text("#!/bin/sh\n" + body + "\n")
            cmd.chmod(0o755)

        (release / ".venv" / "bin").mkdir(parents=True)
        python = release / ".venv" / "bin" / "python"
        python.write_text("""#!/bin/sh
case "$*" in
  *"-m pip"*)
    echo "pip is not installed in uv-managed virtualenv" >&2
    exit 1
    ;;
  *"sys.version_info"*)
    exit 0
    ;;
  *"import aiogram, fastapi, uvicorn, psycopg"*)
    exit 0
    ;;
  *"health/ready"*)
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
""")
        python.chmod(0o755)
        return root, release, current, shared, bins, canary

    def test_simple_install_uses_shared_env_and_venv_without_compose(self):
        with tempfile.TemporaryDirectory() as directory:
            root, release, current, shared, bins, canary = self._create_layout(directory)

            # Invocation without NODE_PLANE_* overrides
            env = {k: v for k, v in os.environ.items() if not k.startswith("NODE_PLANE_")}
            env["PATH"] = f"{bins}:{env.get('PATH', '')}"

            # 1. Invoke from current symlink
            res_current = subprocess.run(
                ["bash", str(current / "scripts" / "healthcheck.sh")],
                env=env, text=True, capture_output=True, timeout=10
            )
            self.assertEqual(res_current.returncode, 0, res_current.stdout + res_current.stderr)
            self.assertIn("Mode: simple", res_current.stdout)
            self.assertIn("Failures: 0", res_current.stdout)
            self.assertIn("Warnings: 0", res_current.stdout)
            self.assertIn("Setup looks healthy", res_current.stdout)
            self.assertNotIn("[FAIL]", res_current.stdout)
            self.assertNotIn("[WARN]", res_current.stdout)
            self.assertNotIn("Compose", res_current.stdout)
            self.assertIn(f"Environment file is readable: {shared / '.env'}", res_current.stdout)
            self.assertIn(f"shared environment file exists at {shared / '.env'}", res_current.stdout)

            # Secrets must never appear in stdout or stderr
            for secret in ("secret_bot_token_98765", "secret_pass_12345", "adapter-secret-token"):
                self.assertNotIn(secret, res_current.stdout)
                self.assertNotIn(secret, res_current.stderr)

            # Environment file must be treated as data, never eval'ed
            self.assertFalse(canary.exists(), "Shell command inside .env was unexpectedly executed")

            # 2. Invoke from physical release with no NODE_PLANE_* overrides
            res_physical = subprocess.run(
                ["bash", str(release / "scripts" / "healthcheck.sh")],
                env=env, text=True, capture_output=True, timeout=10
            )
            self.assertEqual(res_physical.returncode, 0, res_physical.stdout + res_physical.stderr)
            self.assertIn("Failures: 0", res_physical.stdout)
            self.assertIn("Warnings: 0", res_physical.stdout)
            self.assertIn("Setup looks healthy", res_physical.stdout)
            self.assertNotIn("[FAIL]", res_physical.stdout)
            self.assertNotIn("[WARN]", res_physical.stdout)
            for secret in ("secret_bot_token_98765", "secret_pass_12345", "adapter-secret-token"):
                self.assertNotIn(secret, res_physical.stdout)
                self.assertNotIn(secret, res_physical.stderr)
            self.assertFalse(canary.exists())

    def test_missing_environment_and_failed_dependencies_retain_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root, release, current, shared, bins, canary = self._create_layout(directory)
            env = {k: v for k, v in os.environ.items() if not k.startswith("NODE_PLANE_")}
            env["PATH"] = f"{bins}:{env.get('PATH', '')}"

            # Missing environment file triggers failure
            (shared / ".env").unlink()
            res_missing_env = subprocess.run(
                ["bash", str(current / "scripts" / "healthcheck.sh")],
                env=env, text=True, capture_output=True, timeout=10
            )
            self.assertNotEqual(res_missing_env.returncode, 0)
            self.assertIn("[FAIL]", res_missing_env.stdout)
            self.assertIn("Environment file is missing or unreadable", res_missing_env.stdout)

            # Recreate environment, but virtualenv fails dependency imports
            (shared / ".env").write_text(
                "BOT_TOKEN=test\nADMIN_IDS=42\nDB_BACKEND=postgres\nPOSTGRES_DSN=postgresql://test\n"
                f"NODE_PLANE_BACKEND_ADAPTER_TOKEN_FILE={shared / 'data/telegram-adapter.token'}\n"
            )
            failing_python = release / ".venv" / "bin" / "python"
            working_python = failing_python.read_text()
            failing_python.write_text("""#!/bin/sh
case "$*" in
  *"import aiogram"*)
    exit 1
    ;;
  *)
    exit 0
    ;;
esac
""")
            failing_python.chmod(0o755)
            res_dep_fail = subprocess.run(
                ["bash", str(current / "scripts" / "healthcheck.sh")],
                env=env, text=True, capture_output=True, timeout=10
            )
            self.assertNotEqual(res_dep_fail.returncode, 0)
            self.assertIn("[FAIL]", res_dep_fail.stdout)
            self.assertIn("Controller Python dependencies are missing or cannot be imported", res_dep_fail.stdout)

            # Explicit shared override pointing to non-existent directory
            failing_python.write_text(working_python)
            env_override = dict(env, NODE_PLANE_SHARED_DIR=str(root / "nonexistent_shared"))
            res_override_fail = subprocess.run(
                ["bash", str(current / "scripts" / "healthcheck.sh")],
                env=env_override, text=True, capture_output=True, timeout=10
            )
            self.assertNotEqual(res_override_fail.returncode, 0)
            self.assertIn("[FAIL]", res_override_fail.stdout)
            self.assertIn(f"Environment file is missing or unreadable: {root / 'nonexistent_shared/.env'}", res_override_fail.stdout)

            # A valid explicit shared override wins over inferred shared storage.
            custom_shared = root / "custom shared"
            shutil.copytree(shared, custom_shared)
            with (custom_shared / ".env").open("a") as stream:
                stream.write(f'NODE_PLANE_BASE_DIR="{root}"\nNODE_PLANE_APP_DIR="{current}"\n')
            env_override["NODE_PLANE_SHARED_DIR"] = str(custom_shared)
            res_override = subprocess.run(
                ["bash", str(current / "scripts" / "healthcheck.sh")],
                env=env_override, text=True, capture_output=True, timeout=10
            )
            self.assertEqual(res_override.returncode, 0, res_override.stdout + res_override.stderr)
            self.assertIn(f"Environment file is readable: {custom_shared / '.env'}", res_override.stdout)
            self.assertIn("Warnings: 0", res_override.stdout)

    def test_missing_unit_is_not_reported_as_installed(self):
        script = (ROOT / "scripts/healthcheck.sh").read_text()
        start = script.index("systemd_unit_installed() {")
        end = script.index("\ndetect_mode()", start)
        command = 'has_cmd() { return 0; }; systemctl() { echo not-found; };\n' + script[start:end] + '\nsystemd_unit_installed'
        result = subprocess.run(["bash", "-c", command], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
