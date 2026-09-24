import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import unittest


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "runtime_assets" / "awg-add-user.sh"
PROFILE_TOOL = SCRIPT.parent / "awg_profile.py"


class AwgAddUserScriptTests(unittest.TestCase):
    def test_allocates_ip_with_nounset_and_passes_remote_temp_file_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            node_env = root / "node.env"
            config = root / "wg0.conf"
            docker_log = root / "docker.log"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            profile = subprocess.check_output([sys.executable, str(PROFILE_TOOL), "init", str(config)], text=True)
            config.write_text("[Interface]\nPrivateKey = serverprivate\nAddress = 10.8.1.1/24\nListenPort = 51820\n" + profile, encoding="utf-8")
            node_env.write_text(
                f"AWG_CONFIG={shlex.quote(str(config))}\n"
                "AWG_SERVER_IP=203.0.113.10\n"
                f"AWG_PROFILE_TOOL={shlex.quote(str(PROFILE_TOOL))}\n"
                "AWG_CONF2VPN=/nonexistent/conf2vpn.py\n",
                encoding="utf-8",
            )
            docker = fake_bin / "docker"
            docker.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >> {shlex.quote(str(docker_log))}\n"
                "case \"$1\" in\n"
                "  info) exit 0 ;;\n"
                "  ps) echo amnezia-awg ;;\n"
                "  exec)\n"
                "    case \"$*\" in\n"
                "      *allowed-ips*) echo 10.8.1.1 ;;\n"
                "      *genkey*) echo 'private public preshared' ;;\n"
                "      *public-key*) echo serverpublic ;;\n"
                "    esac ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            docker.chmod(0o755)
            runnable = root / "awg-add-user.sh"
            runnable.write_text(
                SCRIPT.read_text(encoding="utf-8").replace(
                    "source /etc/node-plane/node.env",
                    f"source {shlex.quote(str(node_env))}",
                    1,
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            result = subprocess.run(
                ["bash", str(runnable), "alice"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Address = 10.8.1.2/32", result.stdout)
            self.assertIn("AllowedIPs = 10.8.1.2/32", config.read_text(encoding="utf-8"))
            self.assertIn("tmp=$(mktemp)", docker_log.read_text(encoding="utf-8"))
            self.assertIn("HeaderProtectionKey = ", result.stdout)
            self.assertIn("RandomTrailers = on", result.stdout)
            self.assertIn("I5 = ", result.stdout)


if __name__ == "__main__":
    unittest.main()
