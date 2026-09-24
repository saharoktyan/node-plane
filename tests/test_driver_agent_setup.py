import os
import pathlib
import subprocess
import tempfile
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SETUP_SCRIPT = REPO_ROOT / "scripts" / "setup_driver_agents.sh"


class DriverAgentSetupTests(unittest.TestCase):
    def test_dry_run_uses_installed_registry_and_preserves_empty_ssh_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            app_root = root / "current"
            shared_root = root / "shared"
            fake_bin = root / "bin"
            for directory in (
                app_root / "rust" / "node-driver",
                app_root / "rust" / "node-agent",
                app_root / "app" / "services",
                shared_root,
                fake_bin,
            ):
                directory.mkdir(parents=True, exist_ok=True)
            (app_root / "app" / "services" / "server_registry.py").write_text(
                """from dataclasses import dataclass

@dataclass
class Server:
    key: str = 'node-one'
    transport: str = 'ssh'
    ssh_target: str = '203.0.113.10'
    ssh_host: str = '203.0.113.10'
    ssh_port: int = 2222
    ssh_user: str = ''
    ssh_key_path: str = ''
    public_host: str = 'vpn.example.test'

def list_servers(include_disabled=False):
    return [Server()]
""",
                encoding="utf-8",
            )
            (shared_root / ".env").write_text("BOT_TOKEN=test\n", encoding="utf-8")
            ssh_log = root / "ssh.log"
            for command in ("ssh", "scp", "sudo"):
                path = fake_bin / command
                path.write_text(
                    f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{ssh_log}'\n",
                    encoding="utf-8",
                )
                path.chmod(0o755)

            environment = os.environ.copy()
            environment.update(
                NODE_PLANE_APP_DIR=str(app_root),
                NODE_PLANE_SHARED_DIR=str(shared_root),
                PATH=f"{fake_bin}:{environment['PATH']}",
            )
            result = subprocess.run(
                [str(SETUP_SCRIPT), "--dry-run", "--bin-source", "build"],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Dry-run SSH check passed for node-one", result.stdout)
            self.assertIn("node-one=vpn.example.test:50061", result.stdout)
            self.assertIn("203.0.113.10", ssh_log.read_text(encoding="utf-8"))
            self.assertNotIn("vpn.example.test", ssh_log.read_text(encoding="utf-8"))
            self.assertEqual((shared_root / ".env").read_text(encoding="utf-8"), "BOT_TOKEN=test\n")


if __name__ == "__main__":
    unittest.main()
