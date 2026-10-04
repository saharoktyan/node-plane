import contextlib
import base64
import importlib.util
import io
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import zlib


ASSETS = pathlib.Path(__file__).resolve().parents[1] / "runtime_assets"
sys.path.insert(0, str(ASSETS))
import awg_profile  # noqa: E402
import conf2vpn  # noqa: E402


REFRESH_SPEC = importlib.util.spec_from_file_location("refresh_awg_config", ASSETS / "refresh-awg-config.py")
refresh_awg_config = importlib.util.module_from_spec(REFRESH_SPEC)
REFRESH_SPEC.loader.exec_module(refresh_awg_config)


class AwgProfile31Tests(unittest.TestCase):
    def test_all_presets_generate_valid_complete_profiles(self):
        for preset in ('quic', 'dns', 'chaos'):
            with self.subTest(preset=preset):
                for _ in range(20):
                    profile = awg_profile.new_profile(preset)
                    awg_profile.validate(profile)
                    self.assertTrue(all(profile[f'I{i}'] for i in range(1, 6)))
                    if preset == 'quic':
                        self.assertEqual(profile['I1'], '<b 0xc000000001><rc 8><r 900>')
                    elif preset == 'dns':
                        self.assertEqual(profile['I1'], '<rc 2><b 0x01000001000000000000><r 32>')
                    else:
                        self.assertRegex(profile['I1'], r'^<b 0x[0-9a-f]{8}><rc 4><r [0-9]+>$')
                        self.assertTrue(500 <= int(profile['I1'].split('<r ')[1].rstrip('>')) <= 900)
                self.assertNotEqual(awg_profile.new_profile(preset)['HeaderProtectionKey'],
                                    awg_profile.new_profile(preset)['HeaderProtectionKey'])
        with self.assertRaises(ValueError):
            awg_profile.new_profile('invalid')

    def test_server_private_key_uses_wireguard_format(self):
        key = base64.b64decode(awg_profile.new_private_key(), validate=True)
        self.assertEqual(len(key), 32)
        self.assertEqual(key[0] & 7, 0)
        self.assertEqual(key[31] & 0x80, 0)
        self.assertEqual(key[31] & 0x40, 0x40)

    def test_init_awg_does_not_need_host_wg_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            tools = root / "bin"
            tools.mkdir()
            for name in ("mkdir", "dirname", "mktemp", "rm", "chmod", "mv"):
                (tools / name).symlink_to(shutil.which(name))
            (tools / "python3").symlink_to(sys.executable)

            config = root / "wg0.conf"
            node_env = root / "node.env"
            node_env.write_text(
                f"AWG_CONFIG={shlex.quote(str(config))}\n"
                f"AWG_PROFILE_TOOL={shlex.quote(str(ASSETS / 'awg_profile.py'))}\n"
                "AWG_SERVER_ADDRESS=10.8.1.1/24\n",
                encoding="utf-8",
            )
            script = root / "init-awg.sh"
            script.write_text(
                (ASSETS / "init-awg.sh").read_text(encoding="utf-8").replace(
                    "source /etc/node-plane/node.env",
                    f"source {shlex.quote(str(node_env))}",
                    1,
                ),
                encoding="utf-8",
            )
            subprocess.run(
                ["/bin/bash", str(script)],
                env={**os.environ, "PATH": str(tools)},
                capture_output=True,
                text=True,
                check=True,
            )
            values = awg_profile.interface_values(config.read_text(encoding="utf-8"))
            self.assertEqual(len(base64.b64decode(values["PrivateKey"], validate=True)), 32)
            awg_profile.validate(values)
            previous = config.read_text()
            subprocess.run(['/bin/bash', str(script)], env={**os.environ, 'PATH': str(tools)}, check=True, capture_output=True)
            self.assertEqual(config.read_text(), previous)
            # A clean reinstall must generate fresh keys/entropy, while keep-config
            # reinstall and repeated bootstrap preserve the existing identities.
            config.unlink()
            subprocess.run(['/bin/bash', str(script)], env={**os.environ, 'PATH': str(tools)}, check=True, capture_output=True)
            regenerated = awg_profile.interface_values(config.read_text())
            awg_profile.validate(regenerated)
            self.assertNotEqual(regenerated['PrivateKey'], values['PrivateKey'])
            self.assertNotEqual(regenerated['HeaderProtectionKey'], values['HeaderProtectionKey'])
            self.assertTrue(3 <= int(regenerated['Jc']) <= 7)
            self.assertTrue(all(16 <= int(regenerated[f'S{i}']) <= 32 for i in range(1,5)))
            self.assertEqual([regenerated[f'H{i}'] for i in range(1,5)], ['1','2','3','4'])

    def test_legacy_migration_keeps_peer_and_i_sequence_and_refreshes_both_exports(self):
        old_server = """[Interface]
PrivateKey = serverprivate
Address = 10.8.1.1/24
ListenPort = 51820
Jc = 4
Jmin = 40
Jmax = 120
S1 = 12
S2 = 14
S3 = 16
S4 = 18
H1 = 100
H2 = 200
H3 = 300
H4 = 400
I1 = <b 0xc000000001><rc 8><r 1000>
I2 = <b 0x40><rc 4><r 100>
I3 = <r 1200>
I4 = <r 100>
I5 = <r 1200>

[Peer]
PublicKey = clientpublic
PresharedKey = clientpsk
AllowedIPs = 10.8.1.2/32
"""
        migrated = awg_profile.migrate(old_server, "quic")
        values = awg_profile.interface_values(migrated)
        awg_profile.validate(values)
        self.assertEqual(awg_profile.protocol_version(values), "3.1")
        self.assertEqual(values["I1"], "<b 0xc000000001><rc 8><r 1000>")
        self.assertEqual(values["I3"], "<r 1000>")
        self.assertEqual(values["I5"], "<r 1000>")
        self.assertIn("PublicKey = clientpublic", migrated)
        self.assertEqual(awg_profile.migrate(migrated, "quic"), migrated)

        old_client = """[Interface]
PrivateKey = clientprivate
PublicKey = clientpublic
Address = 10.8.1.2/32
DNS = 1.1.1.1
MTU = 1280

[Peer]
PublicKey = oldserverpublic
PresharedKey = clientpsk
Endpoint = old.example:51820
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
"""
        fresh = refresh_awg_config.refresh(old_client, migrated, "new.example", "51820", "newserverpublic")
        self.assertEqual(awg_profile.interface_values(fresh)["HeaderProtectionKey"], values["HeaderProtectionKey"])
        self.assertEqual(awg_profile.interface_values(fresh)["I5"], "<r 1000>")
        self.assertIn("Endpoint = new.example:51820", fresh)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            conf = root / "client.conf"
            output = root / "client.json"
            conf.write_text(fresh, encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                conf2vpn.main(conf, ASSETS / "awg-template.json", output, ASSETS / "amnezia-config-decoder.py",
                              description="Latvia #1 AmneziaWG · alice")
            self.assertTrue(printed.getvalue().startswith("vpn://"))
            payload = json.loads(output.read_text(encoding="utf-8"))["containers"][0]["awg"]
            exported = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(exported["description"], "Latvia #1 AmneziaWG · alice")
            uri = printed.getvalue().strip().removeprefix("vpn://")
            encoded = base64.urlsafe_b64decode(uri + "=" * (-len(uri) % 4))
            imported = json.loads(zlib.decompress(encoded[4:]))
            self.assertEqual(imported["description"], "Latvia #1 AmneziaWG · alice")
            self.assertEqual(exported["defaultContainer"], "amnezia-awg2")
            self.assertEqual(exported["containers"][0]["container"], "amnezia-awg2")
            self.assertEqual(payload["protocol_version"], "3.1")
            self.assertEqual(payload["HeaderProtectionKey"], values["HeaderProtectionKey"])
            self.assertEqual(payload["I5"], "<r 1000>")
            self.assertEqual(json.loads(payload["last_config"])["RandomTrailers"], "on")

    def test_rejects_invalid_header_and_cps(self):
        profile = awg_profile.new_profile("quic")
        profile["S4"] = "8"
        with self.assertRaises(ValueError):
            awg_profile.validate(profile)
        profile = awg_profile.new_profile("quic")
        profile["I5"] = "<r 1200>"
        with self.assertRaises(ValueError):
            awg_profile.validate(profile)


if __name__ == "__main__":
    unittest.main()
