"""Real Xray traffic through the agent's VLESS source limit (VPN plan, section 5).

Run with NODE_PLANE_TEST_DOCKER=1 after building the node agent. All containers
share one network namespace without external connectivity. Two loopback
addresses stand in for two networks of one device.
"""
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

from tests.test_runtime_docker import AGENT_BINARY, XRAY_IMAGE, docker
from tests.test_xray_routing_policy_docker import HELPER_IMAGE, PUBLIC, apply_node_settings

AGENT_IMAGE = 'postgres:16'  # Debian with glibc for the agent binary; already used by the suites.
ALICE, BOB = str(uuid4()), str(uuid4())
# Local port -> (user id, source address). Xray leaves 127.0.0.1 out of its
# online list, so the sources are other loopback addresses.
CLIENTS = {2101: (ALICE, '127.0.0.2'), 2102: (ALICE, '127.0.0.3'), 2103: (BOB, '127.0.0.2')}


def server_config():
    reality = {'show': False, 'dest': 'www.example.com:443', 'xver': 0,
               'serverNames': ['www.example.com'], 'privateKey': 'unused', 'shortIds': ['']}
    clients = [{'id': ALICE, 'email': 'alice'}, {'id': BOB, 'email': 'bob'}]
    inbounds = [{'tag': tag, 'listen': '0.0.0.0', 'port': port, 'protocol': 'vless',
                 'settings': {'decryption': 'none', 'clients': clients},
                 # Plain transport keeps the fixture keyless; routing is transport independent.
                 'streamSettings': {'network': network, 'realitySettings': reality,
                                    **({'xhttpSettings': {'path': '/assets'}} if network == 'xhttp' else {})}}
                for tag, port, network in (('reality-tcp', 443, 'tcp'), ('reality-xhttp', 8443, 'xhttp'))]
    # As before this release: settings apply must add RoutingService and online stats.
    return {'log': {'loglevel': 'warning'}, 'stats': {},
            'policy': {'levels': {'0': {'statsUserUplink': True, 'statsUserDownlink': True}}},
            'api': {'tag': 'api', 'services': ['HandlerService', 'StatsService']},
            'inbounds': [{'tag': 'api', 'listen': '127.0.0.1', 'port': 10085, 'protocol': 'dokodemo-door',
                          'settings': {'address': '127.0.0.1'}}] + inbounds,
            'outbounds': [{'protocol': 'freedom', 'tag': 'direct'}],
            'routing': {'rules': [{'type': 'field', 'inboundTag': ['api'], 'outboundTag': 'api'}]}}


def client_config():
    return {'log': {'loglevel': 'warning'},
            'inbounds': [{'tag': f'in-{local}', 'listen': '127.0.0.1', 'port': local, 'protocol': 'dokodemo-door',
                          'settings': {'address': PUBLIC, 'port': 8080, 'network': 'tcp'}} for local in CLIENTS],
            'outbounds': [{'tag': f'out-{local}', 'protocol': 'vless', 'sendThrough': source,
                           'settings': {'vnext': [{'address': '127.0.0.1', 'port': 443,
                                                   'users': [{'id': user, 'encryption': 'none'}]}]},
                           'streamSettings': {'network': 'tcp'}} for local, (user, source) in CLIENTS.items()],
            'routing': {'rules': [{'type': 'field', 'inboundTag': [f'in-{local}'], 'outboundTag': f'out-{local}'}
                                  for local in CLIENTS]}}


@unittest.skipUnless(os.environ.get('NODE_PLANE_TEST_DOCKER') == '1', 'requires Docker')
@unittest.skipUnless(AGENT_BINARY.is_file(), 'requires a built node agent')
class XrayAdmissionDockerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o755)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.state.chmod(0o777)
        self.prefix = 'np-admission-' + uuid4().hex[:12]

    def container(self, suffix, image, *args, command):
        name = self.prefix + '-' + suffix
        docker('create', '--name', name, *args, '--entrypoint', command[0], image, *command[1:])
        self.addCleanup(lambda: docker('rm', '-f', name, check=False))
        docker('start', name)
        return name

    def start(self):
        self.anchor = self.container('anchor', HELPER_IMAGE, '--network', 'none', '--cap-add', 'NET_ADMIN',
            command=('sh', '-c', f'ip link set lo up; ip addr add {PUBLIC}/32 dev lo; '
                     # Persistent, one child per connection: client connections stay open.
                     f'nc -lk -s {PUBLIC} -p 8080 -e sh -c "cat >> /tmp/received" & sleep 86400'))
        server = self.root / 'server.json'
        server.write_text(json.dumps(server_config()))
        apply_node_settings(server, 'allow')
        config = json.loads(server.read_text())
        self.assertIn('RoutingService', config['api']['services'])
        self.assertTrue(config['policy']['levels']['0']['statsUserOnline'])
        (self.root / 'client.json').write_text(json.dumps(client_config()))
        (self.root / 'client.json').chmod(0o644)
        shared = ('--network', f'container:{self.anchor}', '-v', f'{self.root}:/etc/xray:ro')
        xray = self.container('server', XRAY_IMAGE, *shared, command=('xray', 'run', '-c', '/etc/xray/server.json'))
        self.container('client', XRAY_IMAGE, *shared, command=('xray', 'run', '-c', '/etc/xray/client.json'))
        deadline = time.monotonic() + 10
        while 'started' not in docker('logs', xray).stdout + docker('logs', xray).stderr:
            if time.monotonic() > deadline:
                self.fail(docker('logs', xray).stderr)
            time.sleep(0.1)
        time.sleep(0.5)

    def reaches(self, local):
        """Opens a connection through one client, kept open like a real client's,
        and reports whether its data arrived. Xray lists only sources with live
        connections."""
        marker = uuid4().hex
        docker('exec', '-d', self.anchor, 'sh', '-c', f"(printf '{marker}'; sleep 300) | nc 127.0.0.1 {local}")
        time.sleep(1)
        received = docker('exec', self.anchor, 'sh', '-c', 'cat /tmp/received 2>/dev/null', check=False).stdout
        return marker in received

    def admission_step(self):
        result = subprocess.run(['docker', 'run', '--rm', '--network', f'container:{self.anchor}',
            '-v', f'{AGENT_BINARY}:/agent:ro', '-v', f'{self.state}:/state', '--entrypoint', '/agent',
            AGENT_IMAGE, 'xray-api', 'admission-step', '--state-dir', '/state'],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)['events']

    def policy(self, revision, mode):
        # The journal the ApplyAdmissionPolicy RPC writes on the node.
        (self.state / 'admission-policy.json').write_text(json.dumps({'revision': revision, 'clients': {
            'alice': {'max_sources': 1, 'mode': mode, 'window_seconds': 120, 'cooldown_seconds': 300}}}))

    def test_second_network_evicts_the_first_without_touching_other_users(self):
        self.start()
        self.policy(1, 'observe')
        self.assertTrue(self.reaches(2101))
        self.assertEqual(self.admission_step(), [])
        self.assertTrue(self.reaches(2102))
        # Observe reports the eviction and blocks nothing.
        self.assertEqual(self.admission_step(), [{'kind': 'would_evict', 'email': 'alice', 'source': '127.0.0.2/32'}])
        self.assertTrue(self.reaches(2101))
        self.policy(2, 'enforce')
        self.assertTrue(self.reaches(2102))
        self.assertEqual(self.admission_step(), [{'kind': 'evicted', 'email': 'alice', 'source': '127.0.0.2/32'}])
        # The first network is blocked for alice only; the new one and bob keep working.
        self.assertFalse(self.reaches(2101))
        self.assertTrue(self.reaches(2102))
        self.assertTrue(self.reaches(2103))
        # A blocked source neither counts nor evicts the active one.
        self.assertFalse(self.reaches(2101))
        self.assertEqual(self.admission_step(), [])
        self.assertTrue(self.reaches(2102))
        # Without a policy the block is lifted.
        (self.state / 'admission-policy.json').write_text(json.dumps({'revision': 3, 'clients': {}}))
        self.assertEqual(self.admission_step(), [])
        self.assertTrue(self.reaches(2101))


if __name__ == '__main__':
    unittest.main()
