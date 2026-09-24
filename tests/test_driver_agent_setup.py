import io
import json
import os
import pathlib
import shutil
import subprocess
import tarfile
import tempfile
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SETUP_SCRIPT = REPO_ROOT / "scripts" / "setup_driver_agents.sh"


class DriverAgentSetupTests(unittest.TestCase):
    def test_github_api_downloads_assets_when_direct_links_return_404(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            app_root = root / "current"
            shared_root = root / "shared"
            fake_bin = root / "bin"
            for directory in (app_root / "rust" / "node-driver", app_root / "rust" / "node-agent", shared_root, fake_bin):
                directory.mkdir(parents=True)
            (shared_root / ".env").write_text("BOT_TOKEN=test\n", encoding="utf-8")
            for name, asset_id in (("driver", 101), ("agent", 102)):
                archive = root / f"{name}.tar.gz"
                binary_name = f"node-plane-{name}-linux-amd64"
                with tarfile.open(archive, "w:gz") as output:
                    payload = b"#!/bin/sh\nexit 0\n"
                    info = tarfile.TarInfo(binary_name)
                    info.mode = 0o755
                    info.size = len(payload)
                    output.addfile(info, io.BytesIO(payload))
            metadata = root / "release.json"
            metadata.write_text(json.dumps({"assets": [
                {"name": f"node-plane-{name}-linux-amd64.tar.gz", "state": "uploaded", "url": f"https://api.github.com/repos/example/node-plane/releases/assets/{asset_id}"}
                for name, asset_id in (("driver", 101), ("agent", 102))
            ]}), encoding="utf-8")
            for command in ("ssh", "scp", "sudo"):
                path = fake_bin / command
                path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                path.chmod(0o755)
            curl_log = root / "curl.log"
            curl = fake_bin / "curl"
            curl.write_text("""#!/bin/sh
printf '%s\\n' "$*" >> "$CURL_LOG"
out=''
url=''
previous=''
for arg in "$@"; do
  if [ "$previous" = '-o' ]; then out="$arg"; fi
  case "$arg" in https://*) url="$arg";; esac
  previous="$arg"
done
case "$url" in
  */releases/download/*) exit 22;;
  */releases/tags/*) cp "$METADATA" "$out";;
  */releases/assets/101) cp "$DRIVER_ARCHIVE" "$out";;
  */releases/assets/102) cp "$AGENT_ARCHIVE" "$out";;
  *) exit 1;;
esac
""", encoding="utf-8")
            curl.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                NODE_PLANE_APP_DIR=str(app_root),
                NODE_PLANE_SHARED_DIR=str(shared_root),
                NODE_PLANE_BINARY_RELEASE="v0.4.1-alpha.7",
                PATH=f"{fake_bin}:{environment['PATH']}",
                CURL_LOG=str(curl_log),
                METADATA=str(metadata),
                DRIVER_ARCHIVE=str(root / "driver.tar.gz"),
                AGENT_ARCHIVE=str(root / "agent.tar.gz"),
            )
            result = subprocess.run(
                [str(SETUP_SCRIPT), "--skip-driver", "--skip-agents", "--bin-source", "release"],
                env=environment, text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("trying GitHub API", result.stdout)
            self.assertIn("Accept: application/octet-stream", curl_log.read_text(encoding="utf-8"))

    def test_auto_requires_consent_before_installing_rust_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            app_root = root / "current"
            shared_root = root / "shared"
            fake_bin = root / "bin"
            (app_root / "rust" / "node-driver").mkdir(parents=True)
            (app_root / "rust" / "node-agent").mkdir(parents=True)
            shared_root.mkdir()
            fake_bin.mkdir()
            (shared_root / ".env").write_text("BOT_TOKEN=test\n", encoding="utf-8")
            for command in ("bash", "dirname", "sed", "tail", "mktemp", "rm", "python3"):
                (fake_bin / command).symlink_to(shutil.which(command))
            for command in ("ssh", "scp", "sudo", "apt-get"):
                path = fake_bin / command
                path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                path.chmod(0o755)
            curl = fake_bin / "curl"
            curl.write_text("#!/bin/sh\nexit 22\n", encoding="utf-8")
            curl.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                NODE_PLANE_APP_DIR=str(app_root),
                NODE_PLANE_SHARED_DIR=str(shared_root),
                NODE_PLANE_INSTALL_RUST="no",
                PATH=str(fake_bin),
            )
            result = subprocess.run(
                [str(SETUP_SCRIPT), "--skip-driver", "--skip-agents", "--bin-source", "auto"],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("RUST_INSTALL_REQUIRED", result.stderr)
            self.assertNotIn("Installing Rust build tools", result.stdout)

    def test_auto_dry_run_reports_build_fallback_when_release_assets_are_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            app_root = root / "current"
            shared_root = root / "shared"
            fake_bin = root / "bin"
            (app_root / "rust" / "node-driver").mkdir(parents=True)
            (app_root / "rust" / "node-agent").mkdir(parents=True)
            shared_root.mkdir()
            fake_bin.mkdir()
            (shared_root / ".env").write_text("BOT_TOKEN=test\n", encoding="utf-8")
            for command in ("bash", "dirname", "sed", "tail", "mktemp", "rm", "python3"):
                (fake_bin / command).symlink_to(shutil.which(command))
            for command in ("ssh", "scp", "sudo", "apt-get"):
                path = fake_bin / command
                path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                path.chmod(0o755)
            curl = fake_bin / "curl"
            curl.write_text("#!/bin/sh\nexit 22\n", encoding="utf-8")
            curl.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                NODE_PLANE_APP_DIR=str(app_root),
                NODE_PLANE_SHARED_DIR=str(shared_root),
                PATH=str(fake_bin),
            )
            result = subprocess.run(
                [str(SETUP_SCRIPT), "--dry-run", "--skip-agents", "--bin-source", "auto"],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Rust build tools require confirmation", result.stdout)

    def test_missing_release_asset_stops_before_archive_move(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            app_root = root / "current"
            shared_root = root / "shared"
            fake_bin = root / "bin"
            (app_root / "rust" / "node-driver").mkdir(parents=True)
            (app_root / "rust" / "node-agent").mkdir(parents=True)
            shared_root.mkdir()
            fake_bin.mkdir()
            (shared_root / ".env").write_text("BOT_TOKEN=test\n", encoding="utf-8")
            for command in ("ssh", "scp", "sudo"):
                path = fake_bin / command
                path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                path.chmod(0o755)
            curl = fake_bin / "curl"
            curl.write_text("#!/bin/sh\necho 'fake HTTP 404' >&2\nexit 22\n", encoding="utf-8")
            curl.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                NODE_PLANE_APP_DIR=str(app_root),
                NODE_PLANE_SHARED_DIR=str(shared_root),
                PATH=f"{fake_bin}:{environment['PATH']}",
            )
            result = subprocess.run(
                [str(SETUP_SCRIPT), "--bin-source", "release", "--skip-driver", "--skip-agents"],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("fake HTTP 404", result.stderr)
            self.assertNotIn("mv: cannot stat", result.stderr)

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

    def test_dry_run_includes_local_node_without_ssh(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            app_root = root / "current"
            shared_root = root / "shared"
            fake_bin = root / "bin"
            for directory in (app_root / "rust" / "node-driver", app_root / "rust" / "node-agent", app_root / "app" / "services", shared_root, fake_bin):
                directory.mkdir(parents=True, exist_ok=True)
            (app_root / "app" / "services" / "server_registry.py").write_text(
                """from types import SimpleNamespace
def list_servers(include_disabled=False):
    return [SimpleNamespace(key='home', transport='local')]
""", encoding="utf-8",
            )
            (shared_root / ".env").write_text("BOT_TOKEN=test\n", encoding="utf-8")
            for command in ("ssh", "scp", "sudo"):
                path = fake_bin / command
                path.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
                path.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                NODE_PLANE_APP_DIR=str(app_root),
                NODE_PLANE_SHARED_DIR=str(shared_root),
                PATH=f"{fake_bin}:{environment['PATH']}",
            )
            result = subprocess.run(
                [str(SETUP_SCRIPT), "--dry-run", "--bin-source", "build", "--node-key", "home"],
                env=environment, text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Dry-run local agent check passed for home", result.stdout)
            self.assertIn("home=127.0.0.1:50061", result.stdout)
            self.assertEqual((shared_root / ".env").read_text(encoding="utf-8"), "BOT_TOKEN=test\n")


if __name__ == "__main__":
    unittest.main()
