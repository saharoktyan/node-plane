"""Opt-in real runtime checks; only uniquely named disposable containers.

Requires cached production images and NODE_PLANE_TEST_DOCKER=1. No image builds,
host network, published VPN ports or production node paths are used. Xray
fixtures publish only the Xray API port (and test SOCKS ports), on host loopback.
"""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from backend.removal_verifier import RemovalVerifier
from backend.installation_maintenance import InstallationMaintenance, UNITS

ASSETS = Path(__file__).resolve().parents[1] / 'runtime_assets'
sys.path.insert(0, str(ASSETS))
XRAY_IMAGE = 'ghcr.io/saharoktyan/xray-core:v26.10.31-np.2@sha256:43c919899d9a7daf94f988552d65e98fb8abfcc7b804fb39f94a3812755c08c0'
AGENT_BINARY = Path(__file__).resolve().parents[1] / 'rust/node-agent/target/debug/node-plane-agent'
AWG_IMAGE = 'amneziavpn/amneziawg-go:3.1.20260828@sha256:cbafc02b8373a83f428272db6d8001b37bc02e6211cbd8c0cb4e2e3759b12b72'


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ASSETS / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AWG = load('runtime_test_awg', 'awg_profile.py')


def xray_users(tag):
    """Live users of an inbound, read through the built agent."""
    result = subprocess.run([str(AGENT_BINARY), 'xray-api', f'--server={os.environ["NODE_PLANE_XRAY_API"]}',
                             'inbound-users', f'--tag={tag}'], capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return {user['email']: {'id': (user.get('account') or {}).get('id'),
                            'flow': (user.get('account') or {}).get('flow', '')}
            for user in json.loads(result.stdout).get('users', []) if user.get('email')}


def docker(*args, input=None, check=True):
    return subprocess.run(['docker', *args], input=input, text=True,
        capture_output=True, check=check, timeout=60)


@unittest.skipUnless(os.environ.get('NODE_PLANE_TEST_DOCKER') == '1', 'requires disposable Docker runtime fixtures')
class DockerRuntimeTests(unittest.TestCase):
    def test_xray_image_reference_is_version_and_digest_pinned(self):
        self.assertRegex(XRAY_IMAGE, r'ghcr\.io/saharoktyan/xray-core:v26\.10\.31-np\.2@sha256:[0-9a-f]{64}')
        root = Path(__file__).resolve().parents[1]
        # Deployments and new configs run the agent's release pin.
        self.assertIn(XRAY_IMAGE, (root / 'rust/node-agent/src/runtime/images.rs').read_text())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='node-plane-runtime-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prefix = 'np-test-' + uuid4().hex[:12]
        # The runtime entry points find no agent unless a test opts in
        # (agent_xray).
        absent = patch.dict(os.environ, {'NODE_PLANE_AGENT_BIN': str(self.root / 'no-agent')})
        absent.start()
        self.addCleanup(absent.stop)

    def container(self, suffix, image, *args, command=('sh', '-c', 'sleep 86400')):
        name = self.prefix + '-' + suffix
        identity = docker('create', '--name', name, *args, '--entrypoint', command[0], image,
            *command[1:]).stdout.strip()
        self.addCleanup(lambda: docker('rm', '-f', identity, check=False))
        docker('start', identity)
        return name

    def network(self):
        name = self.prefix + '-network-' + uuid4().hex[:6]
        docker('network', 'create', '--internal', name)
        self.addCleanup(lambda: docker('network', 'rm', name, check=False))
        return name

    def xray(self):
        path, container, _ = self.agent_xray()
        return path, container

    def wait_users(self, container):
        deadline = time.monotonic() + 10
        while True:
            try:
                return xray_users('reality-tcp')
            except RuntimeError:
                if time.monotonic() >= deadline:
                    self.fail(docker('logs', container).stdout)
                time.sleep(0.05)

    def sync(self, path, container, action, identity=None, name='alice'):
        # The agent keeps the config file and the live users in sync.
        result = subprocess.run([str(AGENT_BINARY), 'xray-user', action, str(path), container,
                                 'reality-tcp', 'reality-xhttp', name, *([identity] if identity else [])],
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError(result.stderr.strip())

    def started(self, container):
        return docker('inspect', '-f', '{{.State.StartedAt}}', container).stdout

    def lease_environment(self, **settings):
        """The agent's lease commands on a test node.env, journal and runtime;
        the agent's AWG revocation entry point reads that node.env."""
        node_env = self.root / 'lease-node.env'
        node_env.write_text(''.join(f'{key}={shlex.quote(str(value))}\n' for key, value in settings.items()))
        runtime = self.root / 'lease-runtime'
        runtime.mkdir(exist_ok=True)
        return {**os.environ, 'NODE_PLANE_NODE_ENV': str(node_env), 'NODE_PLANE_RUNTIME_ROOT': str(runtime),
                'NODE_PLANE_PROFILE_JOURNAL': str(self.root / 'lease-journal.sqlite3'),
                'NODE_PLANE_AGENT_BIN': str(AGENT_BINARY)}

    def overdue_lease(self, intent):
        """A granted temporary identity whose lease is overdue, as the agent
        journal records it."""
        import sqlite3
        from datetime import datetime, timedelta, timezone
        with sqlite3.connect(self.root / 'lease-journal.sqlite3') as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL, response TEXT, instance_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fences (protocol TEXT, profile TEXT, revision INTEGER NOT NULL, command_id TEXT NOT NULL, action TEXT, PRIMARY KEY(protocol, profile));
                CREATE TABLE IF NOT EXISTS temporary_leases (protocol TEXT NOT NULL, profile TEXT NOT NULL, command_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL, status TEXT NOT NULL, PRIMARY KEY(protocol, profile));''')
            fingerprint = hashlib.sha256(json.dumps(intent, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            conn.execute("INSERT INTO commands VALUES (?, ?, 'succeeded', ?, 'test')",
                         (intent['command_id'], fingerprint, json.dumps({'summary': 'OK', 'payload_json': ''})))
            conn.execute("INSERT INTO fences VALUES (?, ?, 1, ?, 'ensure')",
                         (intent['protocol'], intent['runtime_name'], intent['command_id']))
            conn.execute("INSERT INTO temporary_leases VALUES (?, ?, ?, ?, 'active')",
                         (intent['protocol'], intent['runtime_name'], intent['command_id'],
                          (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()))

    def expire(self, env):
        result = subprocess.run([str(AGENT_BINARY), 'lease', 'expire'], env=env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    @unittest.skipUnless(AGENT_BINARY.is_file(), 'requires a built node agent')
    def test_expired_temporary_vless_is_pruned_before_real_container_restart(self):
        path, container = self.xray()
        self.sync(path,container,'add',str(uuid4()))
        intent = {'command_id':str(uuid4()),'protocol':'xray','runtime_name':'tmp_'+uuid4().hex,
            'revision':1,'action':'ensure','uuid':str(uuid4()),'short_id':'0123456789abcdef','lease_seconds':43200}
        self.sync(path,container,'add',intent['uuid'],intent['runtime_name'])
        self.overdue_lease(intent)
        docker('stop',container)
        env = self.lease_environment(XRAY_CONFIG=path, XRAY_CONTAINER_NAME=container)
        supervisor = subprocess.Popen([str(AGENT_BINARY),'lease','supervise','xray'],env=env,
                                      stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.addCleanup(lambda: supervisor.poll() is None and supervisor.kill())
        self.wait_users(container)
        for tag in ('reality-tcp','reality-xhttp'):
            users = xray_users(tag)
            self.assertIn('alice',users)
            self.assertNotIn(intent['runtime_name'],users)
        # The supervisor waits on its container and ends with it.
        docker('stop',container)
        _, errors = supervisor.communicate(timeout=60)
        self.assertEqual(supervisor.returncode,0,errors)
        self.assertEqual(self.expire(env),{'expired':1,'pending':0})

    @unittest.skipUnless(AGENT_BINARY.is_file(), 'requires a built node agent')
    def test_xray_api_add_and_revoke_persist_across_real_restart(self):
        path, container = self.xray()
        identity, started = str(uuid4()), self.started(container)
        self.sync(path, container, 'add', identity)
        for tag in ('reality-tcp', 'reality-xhttp'):
            self.assertEqual(xray_users(tag)['alice']['id'], identity)
        self.assertEqual(self.started(container), started)
        docker('restart', container)
        self.wait_users(container)
        for tag in ('reality-tcp', 'reality-xhttp'):
            self.assertEqual(xray_users(tag)['alice']['id'], identity)
        started = self.started(container)
        self.sync(path, container, 'delete')
        self.assertEqual(self.started(container), started)
        docker('restart', container)
        self.wait_users(container)
        for tag in ('reality-tcp', 'reality-xhttp'):
            self.assertNotIn('alice', xray_users(tag))

    def inspect(self, config, protocol, name, *extra, env=()):
        """`inspect-profile.sh` as the profile intent journal runs it."""
        result = subprocess.run([str(AGENT_BINARY), 'inspect-profile', protocol, name, *extra],
            env={**os.environ, 'XRAY_CONFIG': str(config), **dict(env)},
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def agent_xray(self, *publish):
        """Xray with the API published on host loopback and the helpers
        pointed at the built agent. Returns the config path, the container,
        and a runner for `node-plane-agent xray-api`."""
        directory = self.root / 'xray'
        directory.mkdir()
        path = directory / 'config.json'
        config = {
            'log': {'loglevel': 'warning'},
            'stats': {},
            'policy': {'levels': {'0': {'statsUserUplink': True, 'statsUserDownlink': True,
                                        'statsUserOnline': True}}},
            'api': {'tag': 'api', 'services': ['HandlerService', 'StatsService', 'RoutingService']},
            'inbounds': [
                {'tag': tag, 'port': port, 'protocol': 'vless',
                 'settings': {'decryption': 'none', 'clients': []},
                 'streamSettings': {'network': network}}
                for tag, port, network in [('reality-tcp', 443, 'tcp'), ('reality-xhttp', 8443, 'xhttp')]
            ] + [{'tag': 'api', 'listen': '0.0.0.0', 'port': 10085,
                  'protocol': 'dokodemo-door', 'settings': {'address': '127.0.0.1'}}],
            'outbounds': [{'protocol': 'freedom', 'tag': 'direct'},
                          {'protocol': 'blackhole', 'tag': 'node-plane-blocked'}],
            'routing': {'rules': [{'type': 'field', 'inboundTag': ['api'], 'outboundTag': 'api'}]},
        }
        path.write_text(json.dumps(config))
        path.chmod(0o644)
        # A fixed host port for the API: a restarted container keeps it.
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            api_port = probe.getsockname()[1]
        ports = ['-p', f'127.0.0.1:{api_port}:10085',
                 *(arg for port in publish for arg in ('-p', f'127.0.0.1::{port}'))]
        container = self.container('xray-api', XRAY_IMAGE, '--user', '0:0', *ports,
            '-v', f'{directory}:/etc/xray:ro', command=('xray', 'run', '-c', '/etc/xray/config.json'))
        port = api_port
        agent = patch.dict(os.environ, {'NODE_PLANE_AGENT_BIN': str(AGENT_BINARY),
                                        'NODE_PLANE_XRAY_API': f'127.0.0.1:{port}'})
        agent.start()
        self.addCleanup(agent.stop)
        deadline = time.monotonic() + 10
        while True:
            try:
                xray_users('reality-tcp')
                break
            except RuntimeError:
                if time.monotonic() >= deadline:
                    self.fail(docker('logs', container).stdout)
                time.sleep(0.1)
        run = lambda *args: subprocess.run([str(AGENT_BINARY), 'xray-api', f'--server=127.0.0.1:{port}', *args],
                                           capture_output=True, text=True, timeout=15)
        return path, container, run

    @unittest.skipUnless(AGENT_BINARY.is_file(), 'requires a built node agent')
    def test_agent_xray_api_adds_rotates_and_revokes_users_on_real_runtime(self):
        path, container, run = self.agent_xray()

        def cli_users(tag):
            # Independent view through the Xray CLI inside the container.
            raw = docker('exec', container, 'xray', 'api', 'inbounduser',
                         '--server=127.0.0.1:10085', f'-tag={tag}').stdout
            return {user['email']: user['account'] for user in json.loads(raw or '{}').get('users', [])}

        first, second = str(uuid4()), str(uuid4())
        self.sync(path, container, 'add', first)
        self.assertEqual(cli_users('reality-tcp')['alice']['id'], first)
        self.assertEqual(cli_users('reality-tcp')['alice']['flow'], 'xtls-rprx-vision')
        self.assertNotIn('flow', cli_users('reality-xhttp')['alice'])
        self.sync(path, container, 'add', second)
        self.assertEqual(cli_users('reality-xhttp')['alice']['id'], second)
        self.assertTrue(self.inspect(path, 'xray', 'alice', second)['identity_matches'])
        self.assertFalse(self.inspect(path, 'xray', 'alice', first)['identity_matches'])
        self.sync(path, container, 'delete')
        self.assertEqual(set(self.inspect(path, 'xray', 'alice', second).values()), {False})
        self.assertEqual(cli_users('reality-tcp'), {})
        self.assertEqual(cli_users('reality-xhttp'), {})

        added = run('add-rule', '--rule-tag=np-evict-test', '--outbound=node-plane-blocked',
                    '--user=alice', '--source=[2001:db8::7]/64')
        self.assertEqual(added.returncode, 0, added.stderr)
        listed = docker('exec', container, 'xray', 'api', 'lsrules', '--server=127.0.0.1:10085').stdout
        self.assertEqual([rule.get('ruleTag') for rule in json.loads(listed)['rules']], [None, 'np-evict-test'])
        self.assertEqual(run('add-rule', '--rule-tag=np-evict-test', '--outbound=node-plane-blocked',
                             '--user=alice').returncode, 4)
        for _ in range(2):
            self.assertEqual(run('remove-rule', '--rule-tag=np-evict-test').returncode, 0)
        self.assertEqual(json.loads(run('uptime').stdout)['uptime'] >= 0, True)

    @unittest.skipUnless(AGENT_BINARY.is_file(), 'requires a built node agent')
    def test_agent_closes_established_vless_connections_on_real_runtime(self):
        path, server, run = self.agent_xray('1080', '1081')
        identity = str(uuid4())
        added = run('add-user', '--tag=reality-tcp', '--email=alice', f'--uuid={identity}')
        self.assertEqual(added.returncode, 0, added.stderr)
        # A VLESS client in the server's network namespace. Each SOCKS port,
        # published with the server, dials from its own loopback address, so
        # the server sees two sources.
        client_dir = self.root / 'client'
        client_dir.mkdir()
        (client_dir / 'config.json').write_text(json.dumps({
            'log': {'loglevel': 'warning'},
            'inbounds': [{'tag': f's{port}', 'listen': '0.0.0.0', 'port': port, 'protocol': 'socks',
                          'settings': {'udp': False}} for port in (1080, 1081)],
            'outbounds': [{'tag': f'o{port}', 'sendThrough': source, 'protocol': 'vless',
                           'settings': {'vnext': [{'address': '127.0.0.1', 'port': 443,
                               'users': [{'id': identity, 'encryption': 'none'}]}]},
                           'streamSettings': {'network': 'tcp'}}
                          for port, source in ((1080, '127.0.0.2'), (1081, '127.0.0.3'))],
            'routing': {'rules': [{'type': 'field', 'inboundTag': [f's{port}'], 'outboundTag': f'o{port}'}
                                  for port in (1080, 1081)]},
        }))
        (client_dir / 'config.json').chmod(0o644)
        self.container('xray-client', XRAY_IMAGE, '--user', '0:0', '--network', f'container:{server}',
            '-v', f'{client_dir}:/etc/xray:ro', command=('xray', 'run', '-c', '/etc/xray/config.json'))
        socks = {port: int(docker('port', server, f'{port}/tcp').stdout.split()[0].rsplit(':', 1)[1])
                 for port in (1080, 1081)}

        def connect(port=1080):
            # Through VLESS to the server's own API port: an idle HTTP/2
            # connection the gRPC server keeps open.
            deadline = time.monotonic() + 10
            while True:
                stream = socket.create_connection(('127.0.0.1', socks[port]), timeout=5)
                try:
                    stream.sendall(b'\x05\x01\x00')
                    if stream.recv(2) == b'\x05\x00':
                        stream.sendall(b'\x05\x01\x00\x01' + bytes([127, 0, 0, 1]) + (10085).to_bytes(2, 'big'))
                        if stream.recv(10)[:2] == b'\x05\x00':
                            break
                except OSError:
                    pass
                stream.close()
                if time.monotonic() >= deadline:
                    self.fail('SOCKS through VLESS did not connect')
                time.sleep(0.2)
            self.addCleanup(stream.close)
            # The VLESS session is dispatched once the first payload arrives.
            stream.sendall(b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n' + bytes([0, 0, 0, 4, 0, 0, 0, 0, 0]))
            return stream

        def closed(stream, timeout=5):
            stream.settimeout(timeout)
            try:
                while stream.recv(4096):
                    pass
            except (ConnectionResetError, TimeoutError, socket.timeout) as error:
                return not isinstance(error, (TimeoutError, socket.timeout))
            return True

        stream = connect()
        self.assertFalse(closed(stream, 1))
        # Another source closes nothing.
        other = run('close-connections', '--tag=reality-tcp', '--email=alice', '--source=203.0.113.7')
        self.assertEqual(json.loads(other.stdout), {'closed': 0}, other.stderr)
        closing = run('close-connections', '--tag=reality-tcp', '--email=alice', '--source=127.0.0.2')
        self.assertEqual(json.loads(closing.stdout), {'closed': 1}, closing.stderr)
        self.assertTrue(closed(stream))

        # The source limit evicts the older source and closes only its
        # connections.
        state = self.root / 'agent-state'
        state.mkdir()
        (state / 'admission-policy.json').write_text(json.dumps({'revision': 1, 'clients': {'alice': {
            'max_sources': 1, 'mode': 'enforce', 'window_seconds': 60, 'cooldown_seconds': 300}}}))
        step = lambda: json.loads(run('admission-step', f'--state-dir={state}').stdout)['events']
        older = connect(1080)
        self.assertEqual(step(), [])
        time.sleep(1.1)
        newer = connect(1081)
        self.assertEqual(step(), [{'kind': 'evicted', 'email': 'alice', 'source': '127.0.0.2/32'}])
        self.assertTrue(closed(older))
        self.assertFalse(closed(newer, 1))

        # Revocation removes the user and closes what is open.
        self.sync(path, server, 'delete')
        self.assertTrue(closed(newer))
        self.assertNotIn('alice', xray_users('reality-tcp'))

    def cleanup_fixture(self, conflicting=False):
        for directory in ('xray', 'amnezia-awg/data', 'awg-clients', 'unrelated'):
            (self.root / directory).mkdir(parents=True)
            (self.root / directory / 'sentinel').write_text('keep or remove by ownership')
        (self.root / 'helper.py').write_text('preserve runtime helper')
        (self.root / 'journal.sqlite3').write_text('preserve duplicate protection')
        xc, ac = self.prefix + '-xray', self.prefix + '-awg'
        current = self.container('xray', 'postgres:16-alpine', '--network', 'none',
            '-v', f'{self.root / "xray"}:/etc/xray:ro')
        previous = self.container('xray-previous-123', 'postgres:16-alpine', '--network', 'none',
            '-v', f'{self.root / "xray"}:/etc/xray:ro')
        source = self.root / ('unrelated' if conflicting else 'amnezia-awg/data')
        awg = self.container('awg', 'postgres:16-alpine', '--network', 'none',
            '-v', f'{source}:/opt/amnezia/awg')
        unrelated = self.container('unrelated', 'postgres:16-alpine', '--network', 'none',
            '-v', f'{self.root / "unrelated"}:/unrelated')
        node_env = self.root / 'agent' / 'node.env'
        node_env.parent.mkdir()
        node_env.write_text(f'SERVER_KEY=n1\nXRAY_CONFIG={self.root / "xray/config.json"}\n'
                            f'AWG_CONFIG={self.root / "amnezia-awg/data/wg0.conf"}\n'
                            f'XRAY_CONTAINER_NAME={xc}\nAWG_CONTAINER_NAME={ac}\n')
        return node_env, (current, previous, awg), unrelated

    def cleanup(self, node_env):
        """The agent's runtime cleanup of both protocols on the test runtime."""
        intent = {'node_key': 'n1', 'command_id': 'cleanup-1', 'revision': 1,
                  'protocols': ['xray', 'awg'], 'settings': {}}
        env = {**os.environ, 'NODE_PLANE_NODE_ENV': str(node_env), 'NODE_PLANE_RUNTIME_ROOT': str(self.root),
               'NODE_PLANE_PROFILE_JOURNAL': str(node_env.with_name('intents.sqlite3'))}
        return subprocess.run([str(AGENT_BINARY), 'node-operation', 'cleanup_runtime', 'cleanup-1',
                               json.dumps(intent)], env=env, capture_output=True, text=True, timeout=120)

    @unittest.skipUnless(AGENT_BINARY.is_file(), 'requires a built node agent')
    def test_real_docker_cleanup_removes_only_owned_current_previous_and_peer_cache(self):
        node_env, managed, unrelated = self.cleanup_fixture()
        result = self.cleanup(node_env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['result'], {'cleaned': True})
        for name in managed:
            self.assertNotEqual(docker('inspect', name, check=False).returncode, 0)
        self.assertEqual(docker('inspect', unrelated).returncode, 0)
        for directory in ('xray', 'amnezia-awg', 'awg-clients'):
            self.assertFalse((self.root / directory).exists())
        for file in ('helper.py', 'journal.sqlite3', 'unrelated/sentinel'):
            self.assertTrue((self.root / file).exists())

    @unittest.skipUnless(AGENT_BINARY.is_file(), 'requires a built node agent')
    def test_real_docker_unowned_mount_aborts_before_any_container_or_file_deletion(self):
        node_env, managed, unrelated = self.cleanup_fixture(conflicting=True)
        result = self.cleanup(node_env)
        self.assertEqual(result.returncode, 1)
        self.assertIn('ownership', result.stderr)
        for name in (*managed, unrelated):
            self.assertEqual(docker('inspect', '-f', '{{.State.Running}}', name).stdout.strip(), 'true')
        for directory in ('xray', 'amnezia-awg/data', 'awg-clients', 'unrelated'):
            self.assertTrue((self.root / directory / 'sentinel').exists())

    def awg_pair(self, preset, *, timing=None, parameters=None):
        network = self.network()
        keys = [AWG.new_private_key(), AWG.new_private_key()]
        public = [docker('run', '--rm', '-i', '--network', 'none', '--entrypoint', 'awg', AWG_IMAGE,
            'pubkey', input=key + '\n').stdout.strip() for key in keys]
        protection = dict(parameters) if parameters is not None else AWG.new_profile(preset)
        protection.update(timing or {})
        AWG.validate(protection)
        psk = base64.b64encode(os.urandom(32)).decode()
        names = [self.prefix + '-server-' + preset, self.prefix + '-client-' + preset]
        for index, suffix in enumerate(('server-' + preset, 'client-' + preset)):
            directory = self.root / suffix
            directory.mkdir()
            config = '[Interface]\nPrivateKey = ' + keys[index] + '\nListenPort = 51820\n'
            config += '\n'.join(f'{key} = {value}' for key, value in protection.items()) + '\n'
            config += f'[Peer]\nPublicKey = {public[1-index]}\nPresharedKey = {psk}\nAllowedIPs = 10.88.0.{2-index}/32\n'
            if index:
                config += f'Endpoint = {names[0]}:51820\nPersistentKeepalive = 1\n'
            (directory / 'peer.conf').write_text(config)
            # The production image names the tool wg as well.
            script = ('set -eu; ln -sf "$(command -v awg)" /usr/local/bin/wg; mkdir -p /dev/net; mknod /dev/net/tun c 10 200; '
                'amneziawg-go awg0; awg setconf awg0 /config/peer.conf; '
                f'ip addr add 10.88.0.{index+1}/24 dev awg0; ip link set dev awg0 mtu 1280 up; exec sleep 86400')
            self.container(suffix, AWG_IMAGE, '--network', network, '--cap-add', 'NET_ADMIN',
                '--device-cgroup-rule', 'c 10:200 rwm', '-e', 'WG_I_PREFER_BUGGY_USERSPACE_TO_POLISHED_KMOD=1',
                '-v', f'{directory}:/config:ro', '-v', f'{directory}:/opt/amnezia/awg:ro',
                command=('sh', '-c', script))
        return names

    def test_explicit_manual_awg31_profile_transfers_traffic(self):
        profile = AWG.new_profile('quic')
        profile.update(Jc='4', Jmin='50', Jmax='100', H1='10-20', H2='30-40',
            H3='50-60', H4='70-80', I1='<r 80>', RekeyAfterTime='100-110', RejectAfterTime='220-240')
        server, client = self.awg_pair('manual', parameters=profile)
        for size in (64, 1200):
            result = docker('exec', client, 'ping', '-c', '3', '-W', '2', '-s', str(size), '10.88.0.1')
            self.assertIn('0% packet loss', result.stdout)

    def test_awg_all_cps_presets_transfer_small_large_packets_and_parallel_load(self):
        for preset in ('quic', 'dns', 'chaos'):
            with self.subTest(preset=preset):
                server, client = self.awg_pair(preset)
                result = docker('exec', client, 'ping', '-c', '5', '-W', '2', '10.88.0.1', check=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr + docker('logs', client).stderr)
                result = docker('exec', client, 'ping', '-c', '20', '-i', '0.02', '-s', '1100', '-W', '2', '10.88.0.1')
                self.assertIn('0% packet loss', result.stdout)
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=3) as pool:
                    probes = [pool.submit(docker, 'exec', client, 'ping', '-c', '20', '-i', '0.02',
                        '-s', '900', '-W', '2', '10.88.0.1') for _ in range(3)]
                    for probe in probes:
                        self.assertIn('0% packet loss', probe.result().stdout)
                counters = docker('exec', client, 'awg', 'show', 'awg0', 'transfer').stdout.split()
                self.assertTrue(all(int(value) > 0 for value in counters[1:]))

    def test_awg_keepalive_peer_revocation_and_same_identity_reenable(self):
        server, client = self.awg_pair('quic')
        docker('exec', client, 'ping', '-c', '2', '-W', '2', '10.88.0.1')
        before = [int(value) for value in docker('exec', client, 'awg', 'show', 'awg0', 'transfer').stdout.split()[1:]]
        time.sleep(3)
        after = [int(value) for value in docker('exec', client, 'awg', 'show', 'awg0', 'transfer').stdout.split()[1:]]
        # Keepalives are one-way empty packets; no response is required.
        self.assertGreater(after[1], before[1])
        peer = docker('exec', server, 'awg', 'show', 'awg0', 'peers').stdout.strip()
        docker('exec', server, 'awg', 'set', 'awg0', 'peer', peer, 'remove')
        self.assertNotEqual(docker('exec', client, 'ping', '-c', '2', '-W', '1', '10.88.0.1', check=False).returncode, 0)
        docker('exec', server, 'awg', 'setconf', 'awg0', '/config/peer.conf')
        docker('exec', client, 'awg', 'setconf', 'awg0', '/config/peer.conf')
        docker('exec', client, 'ping', '-c', '3', '-W', '2', '10.88.0.1')

    def test_awg_randomized_rekey_preserves_tunnel_across_multiple_handshakes(self):
        # Shorten only this disposable fixture's windows; production defaults
        # and host clocks remain unchanged. Exercise renewal past hard expiry.
        server, client = self.awg_pair('quic', timing={
            'RekeyAfterTime': '3-4', 'RejectAfterTime': '12-15',
            'RekeyTimeout': '1-2', 'KeepaliveTimeout': '1-2',
            'MaxHandshakeAttempts': '8-12',
        })
        docker('exec', client, 'ping', '-c', '1', '-W', '2', '10.88.0.1')
        def handshake():
            values = docker('exec', client, 'awg', 'show', 'awg0', 'latest-handshakes').stdout.split()
            self.assertEqual(len(values), 2)
            return int(values[1])
        first = handshake()
        self.assertGreater(first, 0)
        result = docker('exec', client, 'ping', '-c', '35', '-i', '0.5', '-s', '1100', '-W', '2', '10.88.0.1')
        self.assertIn('0% packet loss', result.stdout)
        self.assertGreaterEqual(handshake() - first, 8)
        self.assertEqual(len(docker('exec', server, 'awg', 'show', 'awg0', 'peers').stdout.split()), 1)

    @unittest.skipUnless(AGENT_BINARY.is_file(), 'requires a built node agent')
    def test_agent_creates_a_valid_xray_config_and_reads_its_public_key(self):
        directory = self.root / 'xray-init'
        path = directory / 'config.json'
        env = {**os.environ, 'XRAY_CONTAINER_NAME': self.prefix + '-absent'}
        created = subprocess.run([str(AGENT_BINARY), 'xray-config', 'init', str(path), 'vpn.example',
                                  'www.example.com', '443', '8443', '/x', '', XRAY_IMAGE],
                                 env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(created.returncode, 0, created.stderr)
        metadata = json.loads(created.stdout)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(json.loads(path.read_text())['inbounds'][1]['streamSettings']['realitySettings']['privateKey'],
                         created.stdout)
        self.assertEqual((metadata['xray_host'], metadata['xray_sni'], metadata['xray_xhttp_path_prefix']),
                         ('vpn.example', 'www.example.com', '/x'))
        # Without a running container the image derives the same public key.
        read = subprocess.run([str(AGENT_BINARY), 'xray-config', 'public', str(path), 'vpn.example', '', XRAY_IMAGE],
                              env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(read.returncode, 0, read.stderr)
        self.assertEqual(json.loads(read.stdout), metadata)
        path.chmod(0o644)
        docker('run', '--rm', '--user', '0:0', '-v', f'{directory}:/etc/xray:ro', XRAY_IMAGE,
               'run', '-test', '-c', '/etc/xray/config.json')

    @unittest.skipUnless(AGENT_BINARY.is_file(), 'requires a built node agent')
    def test_agent_inspects_a_live_awg_peer(self):
        server, client = self.awg_pair('quic')
        # The production image aliases wg to awg; this fixture uses upstream.
        docker('exec', server, 'sh', '-c', 'ln -sf "$(command -v awg)" /usr/local/bin/wg')
        path = self.root / 'server-quic' / 'peer.conf'
        path.write_text(path.read_text().replace('[Peer]', '# n1-alice\n[Peer]', 1))
        clients = self.root / 'clients'
        clients.mkdir()
        (clients / 'n1-alice.txt').write_text('client config')
        env = {'AWG_CONFIG': str(path), 'AWG_CLIENTS_DIR': str(clients), 'AWG_CONTAINER_NAME': server,
               'AWG_IFACE': 'awg0', 'SERVER_KEY': 'n1'}
        self.assertEqual(self.inspect(path, 'awg', 'alice', env=env),
                         dict.fromkeys(('disk_present', 'live_present', 'identity_matches', 'config_available'), True))
        peer = docker('exec', server, 'awg', 'show', 'awg0', 'peers').stdout.strip()
        docker('exec', server, 'awg', 'set', 'awg0', 'peer', peer, 'remove')
        found = self.inspect(path, 'awg', 'alice', env=env)
        self.assertEqual((found['disk_present'], found['live_present'], found['identity_matches']), (True, False, False))

    def test_temporary_awg_expiry_disconnects_peer_and_preserves_other_peer(self):
        server,client = self.awg_pair('quic')
        name = 'tmp_'+uuid4().hex
        path = self.root/'server-quic'/'peer.conf'
        text = path.read_text().replace('[Peer]','# '+name+'\n[Peer]',1)
        permanent_key = base64.b64encode(os.urandom(32)).decode()
        text += '\n# permanent\n[Peer]\nPublicKey = '+permanent_key+'\nAllowedIPs = 10.88.0.3/32\n'
        path.write_text(text)
        docker('exec',server,'awg','setconf','awg0','/config/peer.conf')
        docker('exec',client,'awg','setconf','awg0','/config/peer.conf')
        probe = docker('exec',client,'ping','-c','5','-W','2','10.88.0.1',check=False)
        self.assertEqual(probe.returncode,0,probe.stdout+probe.stderr+docker('logs',client).stderr)
        intent = {'command_id':str(uuid4()),'protocol':'awg','runtime_name':name,'revision':1,
                  'action':'ensure','uuid':'','short_id':'','lease_seconds':43200}
        self.overdue_lease(intent)
        env = self.lease_environment(AWG_CONFIG=path, AWG_CONTAINER_NAME=server, AWG_IFACE='awg0',
                                     AWG_CLIENTS_DIR=self.root/'clients')
        self.assertEqual(self.expire(env),{'expired':1,'pending':0})
        self.assertNotEqual(docker('exec',client,'ping','-c','2','-W','1','10.88.0.1',check=False).returncode,0)
        self.assertEqual(docker('exec',server,'awg','show','awg0','peers').stdout.strip(),permanent_key)
        self.assertNotIn('# '+name,path.read_text())
        self.assertIn('# permanent',path.read_text())

    def uninstall_fixture(self):
        container = self.container('uninstall', 'node-plane-release-builder:bookworm',
            '--network', 'none', '--init')
        self.bot_key = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAExample bot-key'
        self.other_key = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAOtherKey other-key'
        script = '''set -eu
mkdir -p /etc/systemd/system /etc/node-plane/tls /var/lib/node-plane-agent /var/log/node-plane-agent /test /root/.ssh /home/test/.ssh
cp /bin/sleep /usr/local/bin/node-plane-agent
/usr/local/bin/node-plane-agent 86400 &
echo "$!" > /test/agent.pid
sleep 86400 &
echo "$!" > /test/controller.pid
printf '%s\\n' 'node_key = "test"' > /etc/node-plane/agent.toml
for file in profile-intents.sqlite3 profile-intents.sqlite3.lock profile-intents.sqlite3.disabled profile-intents.sqlite3-wal profile-intents.sqlite3-shm profile-intents.sqlite3-journal; do
    echo fixture > /etc/node-plane/"$file"
done
for file in server.crt server.key ca.crt; do echo credential > /etc/node-plane/tls/"$file"; done
mkdir -p /var/lib/node-plane-agent/journal-archives/previous-controller-test
echo old-journal > /var/lib/node-plane-agent/journal-archives/previous-controller-test/profile-intents.sqlite3
echo manifest > /var/lib/node-plane-agent/journal-archives/previous-controller-test/manifest.json
echo fixture > /etc/systemd/system/node-plane-agent.service
echo preserve > /etc/systemd/system/controller-test.service
echo unrelated > /var/lib/node-plane-agent-unrelated
echo preserve > /test/attacker-controlled
ln -s /test/attacker-controlled /root/.ssh/authorized_keys.node-plane-tmp
echo 0123456789abcdef0123456789abcdef > /etc/machine-id
cat > /usr/local/bin/systemctl <<'SYSTEMCTL'
#!/bin/sh
echo "$*" >> /test/systemctl.log
case "$1" in
  disable|daemon-reload) exit 0 ;;
  stop)
    [ ! -e /test/refuse-stop ] || exit 1
    kill "$(cat /test/agent.pid)"
    for attempt in $(seq 1 50); do
      kill -0 "$(cat /test/agent.pid)" 2>/dev/null || exit 0
      sleep 0.02
    done
    exit 1 ;;
  is-active) kill -0 "$(cat /test/agent.pid)" 2>/dev/null ;;
  *) exit 1 ;;
esac
SYSTEMCTL
chmod +x /usr/local/bin/systemctl
'''
        docker('exec', '-i', container, 'sh', '-s', input=script)
        for path in ('/root/.ssh/authorized_keys', '/home/test/.ssh/authorized_keys'):
            docker('exec', '-i', container, 'sh', '-c', 'cat > "$1"', 'fixture', path,
                input=self.bot_key + '\n' + self.other_key + '\n')
        source = (ASSETS.parent / 'rust/node-agent/src/lifecycle/uninstall.rs').read_text()
        function = source[source.index('    pub(crate) fn schedule_uninstall('):]
        uninstall = re.search(r'let script = r#"(.*?)"#;', function, re.S).group(1)
        args = ['/usr/local/bin/node-plane-agent', '/etc/node-plane/agent.toml',
            '/etc/node-plane/tls/server.crt', '/etc/node-plane/tls/server.key', '/etc/node-plane/tls/ca.crt',
            '/var/lib/node-plane-agent', '/var/log/node-plane-agent', '/root/.ssh/authorized_keys', self.bot_key]
        return container, uninstall, args

    def test_uninstall_shell_uses_unique_authorized_keys_temporary_file(self):
        _, script, _ = self.uninstall_fixture()
        self.assertIn('mktemp \"${file}.node-plane-tmp.XXXXXX\"', script)
        self.assertIn('mv -f -- \"$temporary\" \"$file\"', script)
        self.assertNotIn('\"$file.node-plane-tmp\"', script)

    def test_actual_uninstall_shell_removes_credentials_journals_and_binary_preserving_unrelated_resources(self):
        container, script, args = self.uninstall_fixture()
        docker('exec', container, 'sh', '-c', script, 'node-plane-uninstall', *args)
        verifier = RemovalVerifier(local=True, bot_public_key=self.bot_key)
        result = docker('exec', '-i', container, 'sh', '-s', input=verifier.script())
        self.assertIn(verifier.SUCCESS, result.stdout)
        for path in ('/root/.ssh/authorized_keys', '/home/test/.ssh/authorized_keys'):
            self.assertEqual(docker('exec', container, 'cat', path).stdout.strip(), self.other_key)
        self.assertEqual(docker('exec', container, 'cat', '/test/attacker-controlled').stdout.strip(), 'preserve')
        self.assertEqual(docker('exec', container, 'readlink', '/root/.ssh/authorized_keys.node-plane-tmp').stdout.strip(), '/test/attacker-controlled')
        docker('exec', container, 'sh', '-c', 'kill -0 "$(cat /test/controller.pid)"')
        for path in ('/etc/systemd/system/controller-test.service', '/var/lib/node-plane-agent-unrelated'):
            docker('exec', container, 'test', '-f', path)
        calls = docker('exec', container, 'cat', '/test/systemctl.log').stdout
        self.assertIn('disable node-plane-agent.service\nstop node-plane-agent.service\ndaemon-reload', calls)

    def test_actual_uninstall_shell_refuses_to_erase_artifacts_if_service_stop_fails(self):
        container, script, args = self.uninstall_fixture()
        docker('exec', container, 'touch', '/test/refuse-stop')
        self.assertNotEqual(docker('exec', container, 'sh', '-c', script,
            'node-plane-uninstall', *args, check=False).returncode, 0)
        for path in ('/etc/node-plane/profile-intents.sqlite3', '/usr/local/bin/node-plane-agent',
                '/etc/systemd/system/node-plane-agent.service', '/etc/node-plane/tls/server.key'):
            docker('exec', container, 'test', '-f', path)
        docker('exec', container, 'sh', '-c', 'kill -0 "$(cat /test/agent.pid)"')

    def test_controller_generated_uninstall_removes_owned_paths_and_units_only(self):
        container, _, _ = self.uninstall_fixture()
        base, units, stage = self.root / 'installation', self.root / 'units', self.root / 'stage'
        units.mkdir()
        stage.mkdir()
        expected = {'base_dir': str(base), 'shared_dir': str(base / 'shared'),
                    'postgres_container': None, 'units': list(UNITS)}
        host = InstallationMaintenance(staging_root=stage, units_root=units)
        for unit in UNITS:
            text = ('Unit=node-plane-backend-worker.service\n' if unit.endswith('.timer') else
                    f'ExecStart={base}/current/bin\nEnvironmentFile={base}/shared/.env\n')
            (units / unit).write_text(text)
            docker('exec', '-i', container, 'sh', '-c', 'cat > "$1"', 'fixture',
                '/etc/systemd/system/' + unit, input=text)
        with patch.object(host, 'deployment', return_value=expected):
            plan = host.prepare_uninstall(expected, str(uuid4()))
        script = Path(plan['script']).read_text()
        docker('exec', container, 'mkdir', '-p', str(base / 'shared/ssh'), str(Path(plan['script']).parent), '/opt/unrelated-project')
        docker('exec', container, 'touch', str(base / 'shared/.env'), str(base / 'shared/ssh/private-key'),
            '/usr/local/bin/node-plane-driver', '/opt/unrelated-project/sentinel')
        # This process-backed fixture models service stops. The actual systemd
        # transient-unit scheduler is deliberately outside this container test.
        stub = '''#!/bin/sh
echo "$*" >> /test/systemctl.log
if [ "$1" = disable ] && [ "$2" = --now ]; then
    if kill -0 "$(cat /test/controller.pid)" 2>/dev/null; then
        kill "$(cat /test/controller.pid)"
    fi
fi
exit 0
'''
        docker('exec', '-i', container, 'sh', '-c', 'cat > /usr/local/bin/systemctl; chmod +x /usr/local/bin/systemctl', input=stub)
        docker('exec', container, 'bash', '-c', script)
        for path in (str(base), str(Path(plan['script']).parent), '/usr/local/bin/node-plane-driver'):
            self.assertNotEqual(docker('exec', container, 'test', '-e', path, check=False).returncode, 0)
        for unit in UNITS:
            self.assertNotEqual(docker('exec', container, 'test', '-e',
                '/etc/systemd/system/' + unit, check=False).returncode, 0)
        for path in ('/opt/unrelated-project/sentinel', '/etc/systemd/system/controller-test.service',
                '/etc/node-plane/agent.toml', '/usr/local/bin/node-plane-agent'):
            docker('exec', container, 'test', '-f', path)
        docker('exec', container, 'sh', '-c', 'kill -0 "$(cat /test/agent.pid)"')
        self.assertNotEqual(docker('exec', container, 'sh', '-c',
            'kill -0 "$(cat /test/controller.pid)"', check=False).returncode, 0)
