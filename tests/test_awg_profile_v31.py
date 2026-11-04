import base64
import pathlib
import subprocess
import sys
import tempfile
import unittest


ASSETS = pathlib.Path(__file__).resolve().parents[1] / "runtime_assets"
sys.path.insert(0, str(ASSETS))
import awg_profile  # noqa: E402


class AwgProfile31Tests(unittest.TestCase):
    def test_all_presets_generate_valid_complete_profiles(self):
        for preset in ('quic', 'dns', 'chaos'):
            with self.subTest(preset=preset):
                for _ in range(20):
                    profile = awg_profile.new_profile(preset)
                    awg_profile.validate(profile)
                    self.assertTrue(all(profile[field] for field in awg_profile.AWG3_FIELDS))
                    headers = [int(profile[f'H{i}']) for i in range(1, 5)]
                    self.assertEqual(len(set(headers)), 4)
                    self.assertTrue(all(5 <= h <= 2**31 - 1 for h in headers))
                    self.assertNotEqual(int(profile['S1']) + 56, int(profile['S2']))
                    rekey_max = int(profile['RekeyAfterTime'].split('-')[-1])
                    reject_min = int(profile['RejectAfterTime'].split('-')[0])
                    self.assertLess(rekey_max, reject_min)
                    self.assertLess(int(profile['KeepaliveTimeout'].split('-')[-1]), 25)
                    self.assertEqual(profile['DisableCookies'], 'off')
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

    def test_legacy_migration_keeps_peer_and_i_sequence(self):
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

    def test_rejects_invalid_header_and_cps(self):
        profile = awg_profile.new_profile("quic")
        profile["S4"] = "8"
        with self.assertRaises(ValueError):
            awg_profile.validate(profile)
        profile = awg_profile.new_profile("quic")
        profile["I5"] = "<r 1200>"
        with self.assertRaises(ValueError):
            awg_profile.validate(profile)

    def test_rejects_unsafe_padding_and_timing_ranges(self):
        changes = [
            {'S1': '1553'}, {'S2': '1609'}, {'S3': '1637'}, {'S4': '33'},
            {'S1': '16', 'S2': '72'},
            {'RekeyAfterTime': '200-220', 'RejectAfterTime': '100-120'},
            {'RekeyAfterTime': '180', 'RejectAfterTime': ''},
            {'RekeyAfterTime': '', 'RejectAfterTime': '120'},
            {'HeaderProtectionKey': 'invalid'},
        ] + [{field: '0-5'} for field in awg_profile.TIMING_FIELDS]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                awg_profile.validate({**awg_profile.new_profile('quic'), **change})

    def test_padding_boundaries_and_single_positive_timers_are_valid(self):
        profile = awg_profile.new_profile('dns')
        profile.update({field: str(limit) for field, limit in awg_profile.PADDING_LIMITS.items()})
        # Maximum S1/S2 happen to produce equal packet sizes; use S2 one byte lower.
        profile['S2'] = str(awg_profile.PADDING_LIMITS['S2'] - 1)
        profile.update(RekeyAfterTime='120', RejectAfterTime='180', RekeyTimeout='5',
                       KeepaliveTimeout='10', MaxHandshakeAttempts='18')
        awg_profile.validate(profile)

    def test_existing_31_profile_without_randomized_timers_is_not_rotated(self):
        profile = awg_profile.new_profile('quic')
        profile.update({f'S{i}': '24' for i in range(1, 5)})
        profile.update({f'H{i}': str(i) for i in range(1, 5)})
        for field in awg_profile.TIMING_FIELDS:
            profile.pop(field)
        config = '[Interface]\nPrivateKey = existing-server-key\n' + ''.join(
            f'{field} = {value}\n' for field, value in profile.items())
        config += '\n# existing-device\n[Peer]\nPublicKey = existing-peer-key\nPresharedKey = existing-psk\nAllowedIPs = 10.8.1.2/32\n'
        awg_profile.validate(profile)
        self.assertEqual(awg_profile.migrate(config, 'dns'), config)

    def test_explicit_regeneration_preserves_server_and_peer_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'wg0.conf'
            profile = awg_profile.new_profile('quic')
            peers = '\n# device\n[Peer]\nPublicKey = peer-key\nPresharedKey = peer-psk\nAllowedIPs = 10.8.1.2/32\n'
            original = '[Interface]\nPrivateKey = server-key\nListenPort = 443\n' + ''.join(
                f'{field} = {value}\n' for field, value in profile.items()) + peers
            path.write_text(original)
            subprocess.run([sys.executable, str(ASSETS / 'awg_profile.py'), 'regenerate', str(path), 'dns'],
                           check=True, capture_output=True)
            updated = path.read_text()
            self.assertEqual(updated.split('[Peer]', 1)[1], original.split('[Peer]', 1)[1])
            self.assertIn('PrivateKey = server-key\n', updated)
            self.assertIn('ListenPort = 443\n', updated)
            self.assertEqual(path.with_name('wg0.conf.pre-awg31').read_text(), original)
            values = awg_profile.interface_values(updated)
            self.assertNotEqual(values['HeaderProtectionKey'], profile['HeaderProtectionKey'])
            awg_profile.validate(values)


if __name__ == "__main__":
    unittest.main()
