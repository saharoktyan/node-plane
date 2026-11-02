"""Real Xray traffic through node routing policy: private destinations and BitTorrent.

Run with NODE_PLANE_TEST_DOCKER=1. Every container shares one disposable network
namespace without external connectivity; a synthetic public address exists only
on its loopback interface.
"""
import json
import os
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

from tests.test_runtime_docker import XRAY_IMAGE, docker, load

ROOT = Path(__file__).resolve().parents[1]
HELPER_IMAGE = 'postgres:16-alpine'  # BusyBox nc/ip; already required by the Docker suite.
PUBLIC = '93.184.216.34'
CLIENT_ID = str(uuid4())
INTENT = load('routing_policy_intent', 'apply-profile-intent.py')
BITTORRENT_HANDSHAKE = '\\023BitTorrent protocol' + '\\000' * 8 + 'A' * 40


def apply_node_settings(path, bittorrent, ipv6='on'):
    """Run the Xray block of apply-node-settings.sh exactly as shipped."""
    script = (ROOT / 'runtime_assets' / 'apply-node-settings.sh').read_text()
    code = re.search(r"python3 - <<'PY'\n(.*?)\nPY", script, re.S).group(1)
    subprocess.run(['python3', '-c', code], check=True, env={
        **os.environ, 'XRAY_CONFIG_ENV': str(path), 'XRAY_SNI_ENV': 'www.example.com',
        'XRAY_TCP_ENV': '443', 'XRAY_XHTTP_ENV': '8443', 'XRAY_PATH_ENV': '/assets',
        'IPV6_OUTBOUND_ENV': ipv6, 'XRAY_BITTORRENT_ENV': bittorrent})
    path.chmod(0o644)


def server_config():
    reality = {'show': False, 'dest': 'www.example.com:443', 'xver': 0,
               'serverNames': ['www.example.com'], 'privateKey': 'unused', 'shortIds': ['']}
    inbounds = [{'tag': tag, 'listen': '127.0.0.1', 'port': port, 'protocol': 'vless',
                 'settings': {'decryption': 'none', 'clients': [{'id': CLIENT_ID, 'email': 'alice'}]},
                 # Plain transport keeps the fixture keyless; routing is transport independent.
                 'streamSettings': {'network': network, 'realitySettings': reality,
                                    **({'xhttpSettings': {'path': '/assets'}} if network == 'xhttp' else {})}}
                for tag, port, network in (('reality-tcp', 443, 'tcp'), ('reality-xhttp', 8443, 'xhttp'))]
    return {'log': {'loglevel': 'info'}, 'api': {'tag': 'api', 'services': ['HandlerService', 'StatsService']},
            'inbounds': [{'tag': 'api', 'listen': '127.0.0.1', 'port': 10085, 'protocol': 'dokodemo-door',
                          'settings': {'address': '127.0.0.1'}}] + inbounds,
            'outbounds': [{'protocol': 'freedom', 'tag': 'direct'}, {'protocol': 'freedom', 'tag': 'api'}],
            'routing': {'rules': [{'type': 'field', 'inboundTag': ['api'], 'outboundTag': 'api'}]}}


def client_config(targets):
    return {'log': {'loglevel': 'warning'},
            'inbounds': [{'tag': f'to-{port}', 'listen': '127.0.0.1', 'port': local, 'protocol': 'dokodemo-door',
                          'settings': {'address': address, 'port': port, 'network': 'tcp'}}
                         for local, (address, port) in targets.items()],
            'outbounds': [{'protocol': 'vless', 'settings': {'vnext': [{'address': '127.0.0.1', 'port': 443,
                'users': [{'id': CLIENT_ID, 'encryption': 'none'}]}]}, 'streamSettings': {'network': 'tcp'}}]}


@unittest.skipUnless(os.environ.get('NODE_PLANE_TEST_DOCKER') == '1', 'requires Docker')
class XrayRoutingPolicyDockerTests(unittest.TestCase):
    TARGETS = {2001: (PUBLIC, 6881), 2002: (PUBLIC, 8080), 2003: ('127.0.0.1', 18081), 2004: ('127.0.0.1', 10085)}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o755)
        self.prefix = 'np-route-' + uuid4().hex[:12]

    def container(self, suffix, image, *args, command):
        name = self.prefix + '-' + suffix
        docker('create', '--name', name, *args, '--entrypoint', command[0], image, *command[1:])
        self.addCleanup(lambda: docker('rm', '-f', name, check=False))
        docker('start', name)
        return name

    def namespace(self):
        listeners = ' '.join(f'(while true; do nc -l -s {address} -p {port} >> /tmp/received-{port}; done) &'
                             for address, port in ((PUBLIC, 6881), (PUBLIC, 8080), ('127.0.0.1', 18081)))
        return self.container('anchor', HELPER_IMAGE, '--network', 'none', '--cap-add', 'NET_ADMIN',
            command=('sh', '-c', f'ip link set lo up; ip addr add {PUBLIC}/32 dev lo; {listeners} sleep 86400'))

    def start(self, bittorrent):
        anchor = self.namespace()
        directory = self.root / bittorrent
        directory.mkdir()
        directory.chmod(0o755)
        server = directory / 'server.json'
        server.write_text(json.dumps(server_config()))
        apply_node_settings(server, bittorrent)
        INTENT.verify_xray_routing_policy(json.loads(server.read_text()), bittorrent == 'block')
        (directory / 'client.json').write_text(json.dumps(client_config(self.TARGETS)))
        (directory / 'client.json').chmod(0o644)
        shared = ('--network', f'container:{anchor}', '-v', f'{directory}:/etc/xray:ro')
        xray = self.container('server', XRAY_IMAGE, *shared, command=('xray', 'run', '-c', '/etc/xray/server.json'))
        self.container('client', XRAY_IMAGE, *shared, command=('xray', 'run', '-c', '/etc/xray/client.json'))
        deadline = time.monotonic() + 10
        while 'started' not in docker('logs', xray).stdout + docker('logs', xray).stderr:
            if time.monotonic() > deadline:
                self.fail(docker('logs', xray).stderr)
            time.sleep(0.1)
        time.sleep(0.5)
        return anchor, xray

    def send(self, anchor, local, payload):
        docker('exec', anchor, 'sh', '-c', f"printf '{payload}' | timeout 3 nc -w 2 127.0.0.1 {local}", check=False)
        time.sleep(0.5)

    def received(self, anchor, port):
        return docker('exec', anchor, 'sh', '-c', f'cat /tmp/received-{port} 2>/dev/null', check=False).stdout

    def logs(self, container):
        result = docker('logs', container)
        return result.stdout + result.stderr

    def test_private_destinations_are_blocked_by_node_rule_and_bittorrent_follows_policy(self):
        for bittorrent in ('allow', 'block'):
            with self.subTest(bittorrent=bittorrent):
                anchor, xray = self.start(bittorrent)
                self.send(anchor, 2002, 'GET / HTTP/1.0\\r\\n\\r\\n')
                self.assertIn('GET /', self.received(anchor, 8080))
                self.send(anchor, 2003, 'loopback-canary')
                self.assertNotIn('loopback-canary', self.received(anchor, 18081))
                self.send(anchor, 2004, 'api-canary')
                self.send(anchor, 2001, BITTORRENT_HANDSHAKE)
                torrent = self.received(anchor, 6881)
                logs = self.logs(xray)
                # The node rule, not only Freedom's built-in policy, rejects host-local targets.
                self.assertRegex(logs, r'127\.0\.0\.1:18081.*node-plane-blocked|node-plane-blocked.*127\.0\.0\.1:18081')
                if bittorrent == 'block':
                    self.assertNotIn('BitTorrent protocol', torrent)
                else:
                    self.assertIn('BitTorrent protocol', torrent)
                for suffix in ('server', 'client', 'anchor'):
                    docker('rm', '-f', self.prefix + '-' + suffix, check=False)


if __name__ == '__main__':
    unittest.main()
