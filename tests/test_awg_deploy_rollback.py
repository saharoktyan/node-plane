import os
import pathlib
import shlex
import subprocess
import tempfile
import unittest


ASSETS = pathlib.Path(__file__).resolve().parents[1] / "runtime_assets"


class AwgDeployRollbackTests(unittest.TestCase):
    def test_failed_new_container_restores_old_config_and_container(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            config = root / "wg0.conf"
            image_dir = root / "image"
            image_dir.mkdir()
            node_env = root / "node.env"
            node_env.write_text(
                f"AWG_DOCKER_DIR={shlex.quote(str(image_dir))}\n"
                f"AWG_CONFIG={shlex.quote(str(config))}\n"
                "AWG_CONTAINER_NAME=amnezia-awg\n",
                encoding="utf-8",
            )
            profile = subprocess.check_output(
                ["python3", str(ASSETS / "awg_profile.py"), "init", str(config)], text=True
            )
            old_lines = [line for line in profile.splitlines() if not line.startswith((
                "HeaderProtectionKey =", "ContentPaddingAddition =", "RandomTrailers =", "DisableCookies ="
            ))]
            original = "[Interface]\nPrivateKey = oldserver\nAddress = 10.8.1.1/24\nListenPort = 51820\n" + "\n".join(old_lines) + "\n"
            config.write_text(original, encoding="utf-8")
            config.chmod(0o600)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            docker_log = root / "docker.log"
            docker = fake_bin / "docker"
            docker.write_text(
                "#!/bin/sh\n"
                f"echo \"$*\" >> {shlex.quote(str(docker_log))}\n"
                "case \"$1\" in\n"
                "  info|build|rename|stop|start|rm) exit 0 ;;\n"
                "  ps) echo amnezia-awg; exit 0 ;;\n"
                "  run) case \"$2\" in --rm) exit 0 ;; -d) exit 1 ;; esac ;;\n"
                "esac\n"
                "exit 0\n",
                encoding="utf-8",
            )
            docker.chmod(0o755)
            script = root / "deploy-awg.sh"
            script.write_text(
                (ASSETS / "deploy-awg.sh").read_text(encoding="utf-8")
                .replace("source /etc/node-plane/node.env", f"source {shlex.quote(str(node_env))}")
                .replace("if [[ ! -c /dev/net/tun ]]; then", "if false; then")
                .replace("/opt/node-plane-runtime/awg_profile.py", str(ASSETS / "awg_profile.py")),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(config.read_text(encoding="utf-8"), original)
            self.assertTrue(docker_log.exists(), result.stderr)
            calls = docker_log.read_text(encoding="utf-8")
            self.assertIn("rename amnezia-awg amnezia-awg-previous-", calls)
            self.assertIn("rename amnezia-awg-previous-", calls)
            self.assertIn("start amnezia-awg", calls)


if __name__ == "__main__":
    unittest.main()
