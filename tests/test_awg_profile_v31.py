import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest


ASSETS = pathlib.Path(__file__).resolve().parents[1] / "runtime_assets"
sys.path.insert(0, str(ASSETS))
import awg_profile  # noqa: E402
import conf2vpn  # noqa: E402


REFRESH_SPEC = importlib.util.spec_from_file_location("refresh_awg_config", ASSETS / "refresh-awg-config.py")
refresh_awg_config = importlib.util.module_from_spec(REFRESH_SPEC)
REFRESH_SPEC.loader.exec_module(refresh_awg_config)


class AwgProfile31Tests(unittest.TestCase):
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
                conf2vpn.main(conf, ASSETS / "awg-template.json", output, ASSETS / "amnezia-config-decoder.py")
            self.assertTrue(printed.getvalue().startswith("vpn://"))
            payload = json.loads(output.read_text(encoding="utf-8"))["containers"][0]["awg"]
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
