import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BackendSystemdInstallTests(unittest.TestCase):
    def _setup_mock_environment(self, root: Path, base: Path, shared: Path):
        mock_bin = root / "mock_bin"
        mock_bin.mkdir(parents=True, exist_ok=True)
        state_dir = root / "mock_state"
        state_dir.mkdir(parents=True, exist_ok=True)

        id_shim = mock_bin / "id"
        id_shim.write_text("#!/bin/sh\necho 0\nexit 0\n")
        id_shim.chmod(0o755)

        systemctl_shim = mock_bin / "systemctl"
        systemctl_shim.write_text(f"""#!/bin/sh
state_dir="{state_dir}"
echo "$@" >> "$state_dir/systemctl_calls.log"
case "$1" in
  restart|start)
    unit="$2"
    count="$(ls "$state_dir"/env_at_restart_*.txt 2>/dev/null | wc -l)"
    count=$((count + 1))
    if [ -f "{shared}/.env" ]; then
      cp "{shared}/.env" "$state_dir/env_at_restart_${{count}}.txt"
      cp "{shared}/.env" "$state_dir/env_at_${{unit}}_restart.txt"
    fi
    ;;
esac
exit 0
""")
        systemctl_shim.chmod(0o755)

        install_shim = mock_bin / "install"
        install_shim.write_text(f"""#!/bin/sh
echo "$@" >> "{state_dir}/install_calls.log"
for last in "$@"; do :; done
case "$last" in
  /etc/*|/etc)
    exit 0
    ;;
  "{root}"/*)
    mode="0644"
    prev=""
    src=""
    for arg in "$@"; do
      if [ "$prev" = "-m" ]; then
        mode="$arg"
      elif [ "$arg" != "-m" ] && [ -f "$arg" ]; then
        src="$arg"
      fi
      prev="$arg"
    done
    if [ -n "$src" ] && [ -n "$last" ]; then
      cp "$src" "$last"
      chmod "$mode" "$last"
    fi
    exit 0
    ;;
  *)
    exit 98
    ;;
esac
""")
        install_shim.chmod(0o755)

        rm_shim = mock_bin / "rm"
        rm_shim.write_text(f"""#!/bin/sh
echo "$@" >> "{state_dir}/rm_calls.log"
exit 0
""")
        rm_shim.chmod(0o755)

        sleep_shim = mock_bin / "sleep"
        sleep_shim.write_text("#!/bin/sh\nexit 0\n")
        sleep_shim.chmod(0o755)

        python3_symlink = mock_bin / "python3"
        python3_symlink.symlink_to(Path(sys.executable))

        venv_python = base / "current" / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True, exist_ok=True)
        venv_python.write_text("#!/bin/sh\nif [ \"$1\" = \"-c\" ]; then exit 0; fi\nexec python3 \"$@\"\n")
        venv_python.chmod(0o755)

        env = os.environ.copy()
        env["PATH"] = f"{mock_bin}:{env.get('PATH', '')}"
        scratch = root / "tmp"
        scratch.mkdir()
        env["TMPDIR"] = str(scratch)
        return state_dir, env

    @staticmethod
    def _parse_token(env_text: str) -> str | None:
        key = "NODE_PLANE_DRIVER_LOCAL_TOKEN="
        for line in env_text.splitlines():
            if line.startswith(key):
                raw = line[len(key):].strip()
                return raw.strip('"').strip("'")
        return None

    def test_rendered_units_follow_active_release_and_shared_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "install"
            shared = base / "shared"
            (base / "current" / ".venv" / "bin").mkdir(parents=True)
            shared.mkdir()
            (shared / ".env").write_text("DB_BACKEND=postgres\n")
            python = base / "current" / ".venv" / "bin" / "python"
            python.touch(mode=0o700)
            python.chmod(0o700)
            rendered = root / "units"
            subprocess.run(
                [
                    "bash",
                    str(ROOT / "scripts" / "install_backend_systemd.sh"),
                    str(base),
                    str(shared),
                    "--render-dir",
                    str(rendered),
                ],
                check=True,
            )
            marker = rendered / ".node-plane-installation.json"
            ownership = json.loads(marker.read_text())
            self.assertEqual(
                ownership,
                {
                    "format": "node-plane-systemd-v1",
                    "base_dir": str(base.resolve()),
                    "shared_dir": str(shared.resolve()),
                    "postgres_container": None,
                },
            )
            self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
            self.assertFalse((base / ".node-plane-installation.json").exists())
            api = (rendered / "node-plane-backend.service").read_text()
            worker = (rendered / "node-plane-backend-worker.service").read_text()
            self.assertFalse((rendered / "node-plane-backend-worker.timer").exists())
            self.assertIn(f"EnvironmentFile={shared}/.env", api)
            self.assertIn(
                "uvicorn backend.http_api:application --factory --host 127.0.0.1", api
            )
            self.assertIn("ExecStart=" + str(python) + " -m backend.executor", worker)
            self.assertIn(str(shared / "data" / "backend-worker.lock"), worker)
            self.assertIn("Type=simple", worker)
            self.assertIn("--daemon", worker)
            self.assertIn("Restart=on-failure", worker)

            # Render-only mode stays strictly readonly; no credentials generated or written
            env_content = (shared / ".env").read_text()
            self.assertEqual(env_content, "DB_BACKEND=postgres\n")
            self.assertNotIn("NODE_PLANE_DRIVER_LOCAL_TOKEN", env_content)

    def test_fresh_install_prepares_driver_token_before_service_restarts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "install"
            shared = base / "shared"
            shared.mkdir(parents=True)
            env_file = shared / ".env"
            env_file.write_text("DB_BACKEND=postgres\n")
            orig_stat = env_file.stat()

            state_dir, env = self._setup_mock_environment(root, base, shared)

            result = subprocess.run(
                ["bash", str(ROOT / "scripts" / "install_backend_systemd.sh"), str(base), str(shared)],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, f"Script failed: stdout={result.stdout}\nstderr={result.stderr}")

            # Verify systemctl was invoked to restart both services
            calls_log = (state_dir / "systemctl_calls.log").read_text()
            self.assertIn("restart node-plane-backend.service", calls_log)
            self.assertIn("restart node-plane-backend-worker.service", calls_log)

            # Parse snapshot taken at the FIRST restart (node-plane-backend.service)
            first_restart_env = (state_dir / "env_at_restart_1.txt").read_text()
            first_token = self._parse_token(first_restart_env)
            token_present_at_first_restart = bool(first_token)
            self.assertTrue(token_present_at_first_restart, "Token was not present at first backend restart")
            self.assertEqual(len(first_token), 64)
            self.assertTrue(bool(re.fullmatch(r"^[0-9a-f]{64}$", first_token)))

            # Parse snapshot taken at the SECOND restart (node-plane-backend-worker.service)
            second_restart_env = (state_dir / "env_at_restart_2.txt").read_text()
            second_token = self._parse_token(second_restart_env)
            self.assertTrue(bool(second_token))
            tokens_match = (first_token == second_token)
            self.assertTrue(tokens_match)

            # Final .env state, permissions and ownership preservation
            final_env = env_file.read_text()
            self.assertIn("DB_BACKEND=postgres\n", final_env)
            self.assertIn(f"NODE_PLANE_DRIVER_LOCAL_TOKEN={first_token}\n", final_env)

            final_stat = env_file.stat()
            self.assertEqual(final_stat.st_mode & 0o777, 0o600)
            self.assertEqual(final_stat.st_uid, orig_stat.st_uid)
            self.assertEqual(final_stat.st_gid, orig_stat.st_gid)

            # Secret token is never printed to stdout or stderr
            self.assertNotIn(first_token, result.stdout)
            self.assertNotIn(first_token, result.stderr)

            # Confirm installation marker installed to base dir with 0600
            marker = base / ".node-plane-installation.json"
            self.assertTrue(marker.exists())
            self.assertEqual(marker.stat().st_mode & 0o777, 0o600)

            # External calls to /etc were mocked: verify install log had /etc destinations but host /etc was not written
            install_log = (state_dir / "install_calls.log").read_text()
            self.assertIn("node-plane-backend.service /etc/systemd/system/", install_log)
            self.assertIn("node-plane-backend-worker.service /etc/systemd/system/", install_log)

    def test_repeated_install_preserves_same_credential_and_unrelated_env_entries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "install"
            shared = base / "shared"
            shared.mkdir(parents=True)
            existing_token = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
            initial_content = (
                "DB_BACKEND=postgres\n"
                "CUSTOM_SETTING=alpha_value\n"
                f"NODE_PLANE_DRIVER_LOCAL_TOKEN={existing_token}\n"
                "ANOTHER_SETTING=beta_value\n"
            )
            env_file = shared / ".env"
            env_file.write_text(initial_content)
            env_file.chmod(0o600)
            orig_stat = env_file.stat()

            state_dir, env = self._setup_mock_environment(root, base, shared)

            result = subprocess.run(
                ["bash", str(ROOT / "scripts" / "install_backend_systemd.sh"), str(base), str(shared)],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0)

            content = env_file.read_text()
            token = self._parse_token(content)
            token_preserved = (token == existing_token)
            self.assertTrue(token_preserved)
            self.assertIn("CUSTOM_SETTING=alpha_value\n", content)
            self.assertIn("ANOTHER_SETTING=beta_value\n", content)
            self.assertIn("DB_BACKEND=postgres\n", content)
            self.assertEqual(content.count("NODE_PLANE_DRIVER_LOCAL_TOKEN="), 1)

            # Permissions preserved
            st = env_file.stat()
            self.assertEqual(st.st_mode & 0o777, 0o600)
            self.assertEqual(st.st_uid, orig_stat.st_uid)
            self.assertEqual(st.st_gid, orig_stat.st_gid)

            # Token not printed
            self.assertNotIn(existing_token, result.stdout)
            self.assertNotIn(existing_token, result.stderr)

            # Run a second time to verify idempotency on successive executions
            result2 = subprocess.run(
                ["bash", str(ROOT / "scripts" / "install_backend_systemd.sh"), str(base), str(shared)],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result2.returncode, 0)
            content2 = env_file.read_text()
            self.assertEqual(content2, content)
            self.assertEqual(self._parse_token(content2), existing_token)

    def test_blank_and_quoted_empty_token_generates_token(self):
        empty_entries = [
            "NODE_PLANE_DRIVER_LOCAL_TOKEN=",
            'NODE_PLANE_DRIVER_LOCAL_TOKEN=""',
            "NODE_PLANE_DRIVER_LOCAL_TOKEN=''",
            'NODE_PLANE_DRIVER_LOCAL_TOKEN="   "',
            "NODE_PLANE_DRIVER_LOCAL_TOKEN='   '",
        ]
        for empty_entry in empty_entries:
            with self.subTest(entry=empty_entry), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                base = root / "install"
                shared = base / "shared"
                shared.mkdir(parents=True)
                env_file = shared / ".env"
                env_file.write_text(f"DB_BACKEND=postgres\n{empty_entry}\nUNRELATED_ENTRY=active\n")

                state_dir, env = self._setup_mock_environment(root, base, shared)

                result = subprocess.run(
                    ["bash", str(ROOT / "scripts" / "install_backend_systemd.sh"), str(base), str(shared)],
                    env=env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0)

                updated = env_file.read_text()
                token = self._parse_token(updated)
                has_token = bool(token)
                self.assertTrue(has_token)
                self.assertEqual(len(token), 64)
                self.assertTrue(bool(re.fullmatch(r"^[0-9a-f]{64}$", token)))

                # Unrelated entries preserved
                self.assertIn("DB_BACKEND=postgres\n", updated)
                self.assertIn("UNRELATED_ENTRY=active\n", updated)
                self.assertEqual(updated.count("NODE_PLANE_DRIVER_LOCAL_TOKEN="), 1)

                # Permissions 0600
                self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)
                # Token not printed
                self.assertNotIn(token, result.stdout)
                self.assertNotIn(token, result.stderr)

    def test_invalid_credential_entry_aborts_before_service_actions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "install"
            shared = base / "shared"
            shared.mkdir(parents=True)
            env_file = shared / ".env"
            env_file.write_text("DB_BACKEND=postgres\nNODE_PLANE_DRIVER_LOCAL_TOKEN=token extra_word\n")

            state_dir, env = self._setup_mock_environment(root, base, shared)

            result = subprocess.run(
                ["bash", str(ROOT / "scripts" / "install_backend_systemd.sh"), str(base), str(shared)],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Invalid local Driver credential entry", result.stderr)
            # Ensure no systemctl restart or install calls were made
            self.assertFalse((state_dir / "systemctl_calls.log").exists())
            self.assertFalse((state_dir / "install_calls.log").exists())


if __name__ == "__main__":
    unittest.main()
