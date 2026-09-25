import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import unittest


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "runtime_assets" / "awg-add-user.sh"
DELETE_SCRIPT = SCRIPT.parent / "awg-del-user.sh"
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
                f"AWG_CLIENTS_DIR={shlex.quote(str(root / 'clients'))}\n"
                "SERVER_KEY=msk1\n"
                "AWG_SERVER_IP=203.0.113.10\n"
                f"AWG_PROFILE_TOOL={shlex.quote(str(PROFILE_TOOL))}\n"
                f"AWG_CONF2VPN={shlex.quote(str(SCRIPT.parent / 'conf2vpn.py'))}\n"
                f"AWG_TEMPLATE={shlex.quote(str(SCRIPT.parent / 'awg-template.json'))}\n"
                f"AWG_DECODER={shlex.quote(str(SCRIPT.parent / 'amnezia-config-decoder.py'))}\n",
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
                "      *show*allowed-ips*) echo 10.8.1.1 ;;\n"
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
            self.assertIn("\nvpn://", result.stdout)
            self.assertEqual(config.read_text(encoding="utf-8").count("# msk1-alice"), 1)

            cached = root / "clients" / "msk1-alice.txt"
            self.assertEqual(cached.stat().st_mode & 0o777, 0o600)
            first_log = docker_log.read_text(encoding="utf-8")
            repeated = subprocess.run(["bash", str(runnable), "alice"], env=env, text=True, capture_output=True, check=True)
            self.assertEqual(repeated.stdout, result.stdout)
            repeated_log = docker_log.read_text(encoding="utf-8")
            self.assertEqual(repeated_log.count("priv=$(wg genkey)"), first_log.count("priv=$(wg genkey)"))
            self.assertEqual(repeated_log.count("wg set wg0 peer 'public'"), first_log.count("wg set wg0 peer 'public'"))

            cached.unlink()
            replaced = subprocess.run(["bash", str(runnable), "alice"], env=env, text=True, capture_output=True, check=True)
            self.assertIn("Address = 10.8.1.2/32", replaced.stdout)
            self.assertEqual(config.read_text(encoding="utf-8").count("# msk1-alice"), 1)
            self.assertIn("wg set wg0 peer public remove", docker_log.read_text(encoding="utf-8"))

            delete = root / "awg-del-user.sh"
            delete.write_text(
                DELETE_SCRIPT.read_text(encoding="utf-8").replace(
                    "source /etc/node-plane/node.env",
                    f"source {shlex.quote(str(node_env))}",
                    1,
                ),
                encoding="utf-8",
            )
            subprocess.run(["bash", str(delete), "alice"], env=env, text=True, capture_output=True, check=True)
            self.assertNotIn("# msk1-alice", config.read_text(encoding="utf-8"))
            self.assertFalse(cached.exists())


if __name__ == "__main__":
    unittest.main()
