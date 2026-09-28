import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('inspection', Path(__file__).resolve().parents[1] / 'runtime_assets/inspect-profile.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ProfileInspectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'config'
        self.uuid = '11111111-1111-4111-8111-111111111111'

    def test_xray_compares_both_inbounds_and_flow_without_mutation(self):
        api = MODULE.load('xray-user-api')
        tags = ('tcp', 'xhttp')
        config = {'inbounds': [{'tag': tag, 'settings': {'clients': [
            {'email': 'p_test', 'id': self.uuid, **({'flow': 'xtls-rprx-vision'} if tag == 'tcp' else {})}]}} for tag in tags]}
        original = json.dumps(config)
        self.path.write_text(original)
        live = {'tcp': {'p_test': {'id': self.uuid, 'flow': 'xtls-rprx-vision'}},
                'xhttp': {'p_test': {'id': self.uuid, 'flow': ''}}}
        with patch.object(MODULE, 'load', return_value=api), patch.object(api, 'users', side_effect=lambda container, tag: live[tag]):
            value = MODULE.xray(self.path, 'container', tags, 'p_test', self.uuid)
            self.assertTrue(value['identity_matches'])
            self.assertNotIn(self.uuid, json.dumps(value))
            live['xhttp'] = {}
            self.assertFalse(MODULE.xray(self.path, 'container', tags, 'p_test', self.uuid)['identity_matches'])
            live['tcp']['p_test']['flow'] = ''
            self.assertFalse(MODULE.xray(self.path, 'container', tags, 'p_test', self.uuid)['identity_matches'])
        self.assertEqual(self.path.read_text(), original)

    def test_xray_api_error_is_unknown_not_absence(self):
        api = MODULE.load('xray-user-api')
        self.path.write_text(json.dumps({'inbounds': [{'tag': tag, 'settings': {'clients': []}} for tag in ('tcp', 'xhttp')]}))
        with patch.object(MODULE, 'load', return_value=api), patch.object(api, 'users', side_effect=RuntimeError('unavailable')):
            with self.assertRaises(RuntimeError):
                MODULE.xray(self.path, 'container', ('tcp', 'xhttp'), 'p_test', self.uuid)

    def test_awg_checks_live_key_psk_and_allowed_ips(self):
        self.path.write_text('[Interface]\nPrivateKey = server\n\n# node-p_test\n[Peer]\nPublicKey = public\nPresharedKey = secret\nAllowedIPs = 10.0.0.2/32\n')
        (Path(self.tmp.name) / 'node-p_test.txt').write_text('private client config')
        dump = 'server\tpublic\t51820\t0\npublic\tsecret\t(none)\t10.0.0.2/32\t0\t0\t0\t25\n'
        with patch.object(MODULE.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, dump)) as run:
            value = MODULE.awg(self.path, self.tmp.name, 'container', 'wg0', 'node-p_test')
            self.assertTrue(value['identity_matches'])
            self.assertTrue(value['config_available'])
            self.assertEqual(run.call_args.args[0], ['docker', 'exec', 'container', 'wg', 'show', 'wg0', 'dump'])
            self.assertNotIn('secret', json.dumps(value))
            run.return_value.stdout = dump.replace('public\tsecret', 'public\twrong')
            self.assertFalse(MODULE.awg(self.path, self.tmp.name, 'container', 'wg0', 'node-p_test')['identity_matches'])

    def test_awg_unknown_identity_does_not_report_absent(self):
        self.path.write_text('[Interface]\nPrivateKey = server\n')
        with self.assertRaises(ValueError):
            MODULE.awg(self.path, self.tmp.name, 'container', 'wg0', 'missing')

    def test_awg_revoked_archive_verifies_live_removal(self):
        peer = '[Peer]\nPublicKey = public\nPresharedKey = secret\nAllowedIPs = 10.0.0.2/32\n'
        self.path.write_text('[Interface]\nPrivateKey = server\n')
        fingerprint = MODULE.load('awg-peer-state').fingerprint(self.path.read_text())
        (Path(self.tmp.name) / 'node-p_test.revoked.json').write_text(json.dumps({'server': fingerprint, 'peer': peer}))
        with patch.object(MODULE.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'server\tpublic\t51820\t0\n')):
            value = MODULE.awg(self.path, self.tmp.name, 'container', 'wg0', 'node-p_test')
            self.assertFalse(value['disk_present'])
            self.assertFalse(value['live_present'])
