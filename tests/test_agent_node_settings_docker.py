"""Node settings through the built agent and its apply-node-settings.sh entry
point, with fake deployments, Docker and ufw. In the Docker suite, where CI
builds the agent."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from uuid import uuid4

from tests.test_runtime_docker import AGENT_BINARY

ASSETS = Path(__file__).resolve().parents[1] / 'runtime_assets'


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
        (self.root / 'awg_profile.py').write_text((ASSETS / 'awg_profile.py').read_text())
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

    def apply(self, settings):
        command = str(uuid4())
        intent = {'node_key': 'n1', 'command_id': command, 'revision': 1,
                  'protocols': ['awg', 'xray'], 'settings': settings}
        result = subprocess.run([str(AGENT_BINARY), 'node-operation', 'sync_env', command, json.dumps(intent)],
                                env=self.environment, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        # The agent verified node.env and the configs before it succeeded.
        return json.loads(result.stdout)['result']

    def test_real_settings_preserve_identities_and_update_protocol_parameters(self):
        data = {'outbounds': [{'protocol': 'freedom', 'tag': 'direct'}], 'inbounds': [
            {'tag': tag, 'port': port, 'settings': {'clients': [{'id': 'existing-user'}]},
             'streamSettings': {'realitySettings': {'privateKey': 'existing-server-key',
                 'shortIds': ['existing-short-id'], 'serverNames': ['old.example'], 'dest': 'old.example:443'},
                 **({'xhttpSettings': {'path': '/old'}} if tag == 'reality-xhttp' else {})}}
            for tag, port in [('reality-tcp', 443), ('reality-xhttp', 8443)]]}
        self.xray.write_text(json.dumps(data))
        profile = subprocess.check_output(['python3', str(ASSETS / 'awg_profile.py'), 'init', str(self.awg), 'quic'],
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
            ['python3', str(ASSETS / 'awg_profile.py'), 'init', '/dev/null', 'quic'], text=True).splitlines())
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
