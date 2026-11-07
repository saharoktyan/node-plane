"""Node settings through the built agent and its apply-node-settings.sh entry
point, with fake deployments, Docker and ufw. In the Docker suite, where CI
builds the agent."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

from tests.test_runtime_docker import AGENT_BINARY, XRAY_IMAGE



@unittest.skipUnless(os.environ.get('NODE_PLANE_TEST_DOCKER') == '1', 'requires the Docker suite')
class AgentNodeSettingsDockerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.env = self.root / 'node.env'
        self.xray = self.root / 'xray.json'
        self.awg = self.root / 'wg0.conf'
        self.env.write_text(f'SERVER_KEY=n1\nXRAY_CONFIG={self.xray}\nAWG_CONFIG={self.awg}\n'
                            'XRAY_CONTAINER_NAME=xray\nAWG_CONTAINER_NAME=awg\n')
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        # Entry points start this agent; it deploys no containers.
        agent = bin_dir / 'agent'
        agent.write_text(f'#!/bin/sh\n[ "$1" = container-deploy ] && exit 0\nexec {shlex.quote(str(AGENT_BINARY))} "$@"\n')
        agent.chmod(0o755)
        fakes = {
            # Every container runs; nothing is published.
            'docker': bin_dir, 'ufw': bin_dir,
        }
        for name, directory in fakes.items():
            text = ('#!/bin/sh\nif [ "$1" = inspect ]; then echo true; fi\nexit 0\n'
                    if name == 'docker' else '#!/bin/sh\nexit 0\n')
            (directory / name).write_text(text)
        for path in (directory / name for name, directory in fakes.items()):
            path.chmod(0o755)
        self.environment = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}',
            'NODE_PLANE_AGENT_BIN': str(agent), 'NODE_PLANE_RUNTIME_ROOT': str(self.root),
            'NODE_PLANE_NODE_ENV': str(self.env),
            'NODE_PLANE_PROFILE_JOURNAL': str(self.root / 'journal' / 'intents.sqlite3')}

    def apply(self, settings, action='sync_env'):
        command = str(uuid4())
        intent = {'node_key': 'n1', 'command_id': command, 'revision': 1,
                  'protocols': ['awg', 'xray'], 'settings': settings}
        result = subprocess.run([str(AGENT_BINARY), 'node-operation', action, command, json.dumps(intent)],
                                env=self.environment, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        # The agent verified node.env and the configs before it succeeded.
        return json.loads(result.stdout)['result']

    def fresh_image_fixture(self):
        docker = self.root / 'bin' / 'docker'
        docker.write_text(f'''#!{sys.executable}
import json, sys
from pathlib import Path
root = Path({str(self.root)!r})
args = sys.argv[1:]
with (root / 'docker-calls').open('a') as log:
    log.write(json.dumps(args) + '\\n')
upstream = {XRAY_IMAGE!r}
release = 'node-plane-xray:26.10.31-np.2'
identity = 'sha256:' + 'a' * 64
if args[:1] == ['pull']:
    if (root / 'fail-pull').exists():
        sys.exit('registry unavailable')
    if args[1] != upstream:
        sys.exit('The local release tag is not a published image')
    (root / 'downloaded').touch()
elif args[:2] == ['image', 'inspect']:
    if args[-1] == 'custom/xray:test':
        print(identity)
        sys.exit(0)
    if not (root / 'downloaded').exists() or args[-1] != upstream:
        sys.exit('Image is not downloaded')
    print(identity)
elif args[:1] == ['tag']:
    assert args[1:] == [identity, release]
    (root / 'release-tag').touch()
elif args[:2] == ['run', '--rm']:
    if args[2] == 'custom/xray:test':
        assert args[3:] == ['x25519']
        print('PrivateKey: ' + 'A' * 43)
        print('Password: ' + 'B' * 43)
        sys.exit(0)
    if not (root / 'release-tag').exists():
        sys.exit("Unable to find image '" + release + "' locally")
    assert args[2:] in ([release, 'x25519'], [release, 'x25519', '-i', 'A' * 43])
    print('PrivateKey: ' + 'A' * 43)
    print('Password: ' + 'B' * 43)
elif args[:1] == ['inspect']:
    print('true')
elif args[:1] == ['exec']:
    sys.exit('Container is not running')
''')
        docker.chmod(0o755)

    def test_bootstrap_prepares_the_pinned_image_before_initializing_a_clean_runtime(self):
        self.fresh_image_fixture()
        self.apply({'public_host': 'node.example', 'xray_sni': 'www.example.com',
                    'xray_tcp_port': 2443, 'xray_xhttp_port': 8443, 'xray_xhttp_path': '/assets',
                    'awg_port': 51820, 'awg_interface': 'wg0'}, action='bootstrap')
        self.assertTrue(self.xray.is_file())
        self.assertTrue(self.awg.is_file())
        calls = [json.loads(line) for line in (self.root / 'docker-calls').read_text().splitlines()]
        pull = calls.index(['pull', XRAY_IMAGE])
        keygen = calls.index(['run', '--rm', 'node-plane-xray:26.10.31-np.2', 'x25519'])
        self.assertLess(pull, keygen)
        self.assertNotIn(['pull', 'node-plane-xray:26.10.31-np.2'], calls)

    def test_xray_initialization_stops_before_key_generation_when_the_pinned_pull_fails(self):
        self.fresh_image_fixture()
        (self.root / 'fail-pull').touch()
        result = subprocess.run([str(AGENT_BINARY), 'xray-config', 'init', str(self.xray), 'node.example'],
                                env=self.environment, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('registry unavailable', result.stderr)
        self.assertFalse(self.xray.exists())
        calls = [json.loads(line) for line in (self.root / 'docker-calls').read_text().splitlines()]
        self.assertIn(['pull', XRAY_IMAGE], calls)
        self.assertFalse(any(call[0] == 'run' for call in calls))

    def test_xray_initialization_preserves_a_cached_custom_image(self):
        self.fresh_image_fixture()
        result = subprocess.run([str(AGENT_BINARY), 'xray-config', 'init', str(self.xray), 'node.example',
                                 'www.example.com', '2443', '8443', '/assets', 'xtls-rprx-vision', 'custom/xray:test'],
                                env=self.environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.xray.exists())
        calls = [json.loads(line) for line in (self.root / 'docker-calls').read_text().splitlines()]
        self.assertIn(['run', '--rm', 'custom/xray:test', 'x25519'], calls)
        self.assertFalse(any(call[0] in {'pull', 'tag'} for call in calls))

    def test_public_key_derivation_prepares_the_image_when_the_container_is_not_running(self):
        self.fresh_image_fixture()
        initialized = subprocess.run([str(AGENT_BINARY), 'xray-config', 'init', str(self.xray), 'node.example'],
                                     env=self.environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        before = self.xray.read_bytes()
        (self.root / 'downloaded').unlink()
        (self.root / 'release-tag').unlink()
        result = subprocess.run([str(AGENT_BINARY), 'xray-config', 'public', str(self.xray), 'node.example'],
                                env=self.environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.xray.read_bytes(), before)
        self.assertTrue((self.root / 'release-tag').exists())

    def test_real_settings_preserve_identities_and_update_protocol_parameters(self):
        data = {'outbounds': [{'protocol': 'freedom', 'tag': 'direct'}], 'inbounds': [
            {'tag': tag, 'port': port, 'settings': {'clients': [{'id': 'existing-user'}]},
             'streamSettings': {'realitySettings': {'privateKey': 'existing-server-key',
                 'shortIds': ['existing-short-id'], 'serverNames': ['old.example'], 'dest': 'old.example:443'},
                 **({'xhttpSettings': {'path': '/old'}} if tag == 'reality-xhttp' else {})}}
            for tag, port in [('reality-tcp', 443), ('reality-xhttp', 8443)]]}
        self.xray.write_text(json.dumps(data))
        profile = subprocess.check_output([str(AGENT_BINARY), 'awg-profile', 'init', str(self.awg), 'quic'],
                                          text=True)
        peers = '\n# existing-device\n[Peer]\nPublicKey = existing-peer\nPresharedKey = existing-psk\nAllowedIPs = 10.8.1.2/32\n'
        self.awg.write_text('[Interface]\nPrivateKey = existing-server\nListenPort = 51820\n' + profile + peers)
        settings = {'public_host': 'new.example', 'xray_host': 'vless.example', 'xray_sni': 'www.example.com',
            'xray_fingerprint': 'firefox', 'xray_tcp_port': 1443, 'xray_xhttp_port': 2443,
            'xray_xhttp_path': '/new', 'awg_public_host': 'awg.example', 'awg_interface': 'vpn0',
            'awg_port': 5300, 'awg_i1_preset': 'dns', 'awg_port_mode': 'manual', 'ipv6_outbound': 'off'}
        self.apply(settings)
        updated = json.loads(self.xray.read_text())
        for inbound in updated['inbounds']:
            self.assertEqual(inbound['settings']['clients'], [{'id': 'existing-user'}])
            reality = inbound['streamSettings']['realitySettings']
            self.assertEqual((reality['privateKey'], reality['shortIds']), ('existing-server-key', ['existing-short-id']))
        new_awg = self.root / 'vpn0.conf'
        self.assertIn('PrivateKey = existing-server', new_awg.read_text())
        self.assertIn(peers, new_awg.read_text())
        direct = next(item for item in updated['outbounds'] if item['tag'] == 'direct')
        self.assertEqual(direct['settings']['domainStrategy'], 'ForceIPv4')
        self.assertEqual([rule['ip'] for rule in updated['routing']['rules']
                          if rule.get('outboundTag') == 'node-plane-ipv6-disabled'], [['::/0']])
        self.assertNotIn('sniffing', updated['inbounds'][0])

        self.apply({**settings, 'xray_bittorrent': 'block'})
        blocked = json.loads(self.xray.read_text())
        self.assertEqual([rule.get('ruleTag') for rule in blocked['routing']['rules']][:2],
                         ['node-plane-private', 'node-plane-bittorrent'])
        self.assertTrue(all(inbound['sniffing']['routeOnly'] for inbound in blocked['inbounds']))
        self.assertIn('XRAY_BITTORRENT=block', self.env.read_text())

        self.apply({**settings, 'ipv6_outbound': 'on'})
        enabled = json.loads(self.xray.read_text())
        direct = next(item for item in enabled['outbounds'] if item['tag'] == 'direct')
        self.assertEqual(direct['settings']['domainStrategy'], 'AsIs')
        self.assertFalse(any(item.get('outboundTag') == 'node-plane-ipv6-disabled'
                             for item in enabled['routing']['rules']))

        # Explicit parameters may use CPS patterns unrelated to the preset.
        manual = dict(line.split(' = ', 1) for line in subprocess.check_output(
            [str(AGENT_BINARY), 'awg-profile', 'init', '/dev/null', 'quic'], text=True).splitlines())
        manual['I1'] = '<r 80>'
        self.apply({**settings, 'ipv6_outbound': 'on', 'awg_parameters': manual})
        self.assertIn(peers, new_awg.read_text())
        self.assertIn('PrivateKey = existing-server', new_awg.read_text())
        self.assertIn('I1 = <r 80>', new_awg.read_text())
        self.assertEqual((self.root / '.node-plane-i1-preset').read_text().strip(), 'manual')
        self.apply({**settings, 'ipv6_outbound': 'on', 'awg_parameters': {}})
        self.assertEqual((self.root / '.node-plane-i1-preset').read_text().strip(), 'dns')
        self.assertIn('I1 = <rc 2><b 0x01000001000000000000>', new_awg.read_text())
        self.assertIn(peers, new_awg.read_text())


if __name__ == '__main__':
    unittest.main()
