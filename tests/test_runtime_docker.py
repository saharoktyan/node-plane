"""Opt-in real runtime checks; only uniquely named disposable containers.

Requires cached production images and NODE_PLANE_TEST_DOCKER=1. No image builds,
host network, published VPN ports or production node paths are used.
"""
import base64
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from backend.removal_verifier import RemovalVerifier
from backend.installation_maintenance import InstallationMaintenance, UNITS

ASSETS = Path(__file__).resolve().parents[1] / 'runtime_assets'
XRAY_IMAGE = 'ghcr.io/xtls/xray-core:26.3.27'
AWG_IMAGE = 'amneziavpn/amneziawg-go:3.1.20260828@sha256:cbafc02b8373a83f428272db6d8001b37bc02e6211cbd8c0cb4e2e3759b12b72'


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ASSETS / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


XRAY = load('runtime_test_xray', 'xray-user-api.py')
AWG = load('runtime_test_awg', 'awg_profile.py')
OPERATIONS = load('runtime_test_operations', 'backend-node-operation.py')


def docker(*args, input=None, check=True):
    return subprocess.run(['docker', *args], input=input, text=True,
        capture_output=True, check=check, timeout=60)


@unittest.skipUnless(os.environ.get('NODE_PLANE_TEST_DOCKER') == '1', 'requires disposable Docker runtime fixtures')
class DockerRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='node-plane-runtime-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prefix = 'np-test-' + uuid4().hex[:12]

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
        directory = self.root / 'xray'
        directory.mkdir()
        path = directory / 'config.json'
        config = {
            'log': {'loglevel': 'warning'},
            'api': {'tag': 'api', 'services': ['HandlerService', 'StatsService']},
            'inbounds': [
                {'tag': tag, 'port': port, 'protocol': 'vless',
                 'settings': {'decryption': 'none', 'clients': []},
                 'streamSettings': {'network': network}}
                for tag, port, network in [('reality-tcp', 443, 'tcp'), ('reality-xhttp', 8443, 'xhttp')]
            ] + [{'tag': 'api', 'listen': '127.0.0.1', 'port': 10085,
                  'protocol': 'dokodemo-door', 'settings': {'address': '127.0.0.1'}}],
            'outbounds': [{'protocol': 'freedom', 'tag': 'direct'}],
            'routing': {'rules': [{'type': 'field', 'inboundTag': ['api'], 'outboundTag': 'api'}]},
        }
        path.write_text(json.dumps(config))
        container = self.container('xray', XRAY_IMAGE, '--network', 'none', '--user', '0:0',
            '-v', f'{directory}:/etc/xray:ro', command=('xray', 'run', '-c', '/etc/xray/config.json'))
        self.wait_users(container)
        return path, container

    def wait_users(self, container):
        deadline = time.monotonic() + 10
        while True:
            try:
                return XRAY.users(container, 'reality-tcp')
            except RuntimeError:
                if time.monotonic() >= deadline:
                    self.fail(docker('logs', container).stdout)
                time.sleep(0.05)

    def sync(self, path, container, action, identity=None):
        XRAY.run(action, path, container, 'reality-tcp', 'reality-xhttp', 'alice', identity)

    def started(self, container):
        return docker('inspect', '-f', '{{.State.StartedAt}}', container).stdout

    def test_expired_temporary_vless_is_pruned_before_real_container_restart(self):
        path, container = self.xray()
        self.sync(path,container,'add',str(uuid4()))
        engine = OPERATIONS.intents
        journal = self.root/'lease-journal.sqlite3'
        intent = {'command_id':str(uuid4()),'protocol':'xray','runtime_name':'tmp_'+uuid4().hex,
            'revision':1,'action':'ensure','uuid':str(uuid4()),'short_id':'0123456789abcdef','lease_seconds':43200}
        def mutate(value,fd):
            XRAY.run('add' if value['action']=='ensure' else 'delete',path,container,
                     'reality-tcp','reality-xhttp',value['runtime_name'],value['uuid'] or None)
            return {'summary':'OK','payload_json':''}
        engine.apply(intent,journal,mutate)
        import sqlite3
        from datetime import datetime,timedelta,timezone
        with sqlite3.connect(journal) as conn:
            conn.execute('UPDATE temporary_leases SET expires_at=?',((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),))
        docker('stop',container)
        def execute(args,**kwargs):
            if args[:2]==['docker','wait']:
                return subprocess.CompletedProcess(args,0,stdout='0\n')
            return subprocess.run(args,**kwargs)
        with patch.object(engine,'leased_runtime_settings',return_value=(path,container,'')):
            engine.start_leased_runtime('xray',journal,execute)
        self.wait_users(container)
        for tag in ('reality-tcp','reality-xhttp'):
            users = XRAY.users(container,tag)
            self.assertIn('alice',users)
            self.assertNotIn(intent['runtime_name'],users)
        self.assertEqual(engine.expire_leases(journal,mutate),{'expired':1,'pending':0})

    def test_xray_api_add_and_revoke_persist_across_real_restart(self):
        path, container = self.xray()
        identity, started = str(uuid4()), self.started(container)
        self.sync(path, container, 'add', identity)
        for tag in ('reality-tcp', 'reality-xhttp'):
            self.assertEqual(XRAY.users(container, tag)['alice']['id'], identity)
        self.assertEqual(self.started(container), started)
        docker('restart', container)
        self.wait_users(container)
        for tag in ('reality-tcp', 'reality-xhttp'):
            self.assertEqual(XRAY.users(container, tag)['alice']['id'], identity)
        started = self.started(container)
        self.sync(path, container, 'delete')
        self.assertEqual(self.started(container), started)
        docker('restart', container)
        self.wait_users(container)
        for tag in ('reality-tcp', 'reality-xhttp'):
            self.assertNotIn('alice', XRAY.users(container, tag))

    def test_xray_partial_api_add_and_delete_have_durable_repairable_state(self):
        path, container = self.xray()
        identity, started = str(uuid4()), self.started(container)
        original = XRAY.add_live
        def add(container, inbound, client, directory):
            if inbound['tag'] == 'reality-xhttp':
                raise RuntimeError('injected second-inbound API failure')
            return original(container, inbound, client, directory)
        with patch.object(XRAY, 'add_live', side_effect=add):
            with self.assertRaises(RuntimeError):
                self.sync(path, container, 'add', identity)
        self.assertIn('alice', XRAY.users(container, 'reality-tcp'))
        self.assertNotIn('alice', XRAY.users(container, 'reality-xhttp'))
        self.sync(path, container, 'add', identity)
        remove = XRAY.remove_live
        def delete(container, tag, name):
            if tag == 'reality-xhttp':
                raise RuntimeError('injected second-inbound API failure')
            return remove(container, tag, name)
        with patch.object(XRAY, 'remove_live', side_effect=delete):
            with self.assertRaises(RuntimeError):
                self.sync(path, container, 'delete')
        self.assertNotIn('alice', XRAY.users(container, 'reality-tcp'))
        self.assertIn('alice', XRAY.users(container, 'reality-xhttp'))
        self.assertEqual(self.started(container), started)
        docker('restart', container)
        self.wait_users(container)
        for tag in ('reality-tcp', 'reality-xhttp'):
            self.assertNotIn('alice', XRAY.users(container, tag))
        self.sync(path, container, 'delete')

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
        values = [str(self.root / 'xray/config.json'), str(self.root / 'amnezia-awg/data/wg0.conf'), xc, ac]
        return values, (current, previous, awg), unrelated

    def test_real_docker_cleanup_removes_only_owned_current_previous_and_peer_cache(self):
        values, managed, unrelated = self.cleanup_fixture()
        with patch.object(OPERATIONS, 'ROOT', self.root), patch.object(OPERATIONS, 'environment', return_value=values):
            OPERATIONS.remove_protocol_runtime(None)
        for name in managed:
            self.assertNotEqual(docker('inspect', name, check=False).returncode, 0)
        self.assertEqual(docker('inspect', unrelated).returncode, 0)
        for directory in ('xray', 'amnezia-awg', 'awg-clients'):
            self.assertFalse((self.root / directory).exists())
        for file in ('helper.py', 'journal.sqlite3', 'unrelated/sentinel'):
            self.assertTrue((self.root / file).exists())

    def test_real_docker_unowned_mount_aborts_before_any_container_or_file_deletion(self):
        values, managed, unrelated = self.cleanup_fixture(conflicting=True)
        with patch.object(OPERATIONS, 'ROOT', self.root), patch.object(OPERATIONS, 'environment', return_value=values):
            with self.assertRaisesRegex(ValueError, 'ownership'):
                OPERATIONS.remove_protocol_runtime(None)
        for name in (*managed, unrelated):
            self.assertEqual(docker('inspect', '-f', '{{.State.Running}}', name).stdout.strip(), 'true')
        for directory in ('xray', 'amnezia-awg/data', 'awg-clients', 'unrelated'):
            self.assertTrue((self.root / directory / 'sentinel').exists())

    def awg_pair(self, preset):
        network = self.network()
        keys = [AWG.new_private_key(), AWG.new_private_key()]
        public = [docker('run', '--rm', '-i', '--network', 'none', '--entrypoint', 'awg', AWG_IMAGE,
            'pubkey', input=key + '\n').stdout.strip() for key in keys]
        protection = AWG.new_profile(preset)
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
            script = ('set -eu; mkdir -p /dev/net; mknod /dev/net/tun c 10 200; '
                'amneziawg-go awg0; awg setconf awg0 /config/peer.conf; '
                f'ip addr add 10.88.0.{index+1}/24 dev awg0; ip link set dev awg0 mtu 1280 up; exec sleep 86400')
            self.container(suffix, AWG_IMAGE, '--network', network, '--cap-add', 'NET_ADMIN',
                '--device-cgroup-rule', 'c 10:200 rwm', '-e', 'WG_I_PREFER_BUGGY_USERSPACE_TO_POLISHED_KMOD=1',
                '-v', f'{directory}:/config:ro', command=('sh', '-c', script))
        return names

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

    def test_temporary_awg_expiry_disconnects_peer_and_preserves_other_peer(self):
        server,client = self.awg_pair('quic')
        engine = OPERATIONS.intents
        journal = self.root/'lease-journal.sqlite3'
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
        peers = load('expiry_test_awg_peers','awg-peer-state.py')
        intent = {'command_id':str(uuid4()),'protocol':'awg','runtime_name':name,'revision':1,
                  'action':'ensure','uuid':'','short_id':'','lease_seconds':43200}
        def wg_docker(*args,**kwargs):
            # The production wrapper aliases wg to awg; this fixture uses the
            # unmodified upstream image, so select its original executable.
            return subprocess.run(['docker',*('awg' if a=='wg' else a for a in args)],check=True,**kwargs)
        def mutate(value,fd):
            if value['action']=='delete':
                with patch.object(peers,'docker',side_effect=wg_docker):
                    peers.mutate('revoke',path,self.root/'clients',name,server,'awg0')
            return {'summary':'OK','payload_json':''}
        engine.apply(intent,journal,mutate)
        import sqlite3
        from datetime import datetime,timedelta,timezone
        with sqlite3.connect(journal) as conn:
            conn.execute('UPDATE temporary_leases SET expires_at=?',((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),))
        self.assertEqual(engine.expire_leases(journal,mutate),{'expired':1,'pending':0})
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
        source = (ASSETS.parent / 'rust/node-agent/src/main.rs').read_text()
        function = source[source.index('    fn schedule_uninstall('):]
        uninstall = re.search(r'let script = r#"(.*?)"#;', function, re.S).group(1)
        args = ['/usr/local/bin/node-plane-agent', '/etc/node-plane/agent.toml',
            '/etc/node-plane/tls/server.crt', '/etc/node-plane/tls/server.key', '/etc/node-plane/tls/ca.crt',
            '/var/lib/node-plane-agent', '/var/log/node-plane-agent', '/root/.ssh/authorized_keys', self.bot_key]
        return container, uninstall, args

    def test_actual_uninstall_shell_removes_credentials_journals_and_binary_preserving_unrelated_resources(self):
        container, script, args = self.uninstall_fixture()
        docker('exec', container, 'sh', '-c', script, 'node-plane-uninstall', *args)
        verifier = RemovalVerifier(local=True, bot_public_key=self.bot_key)
        result = docker('exec', '-i', container, 'sh', '-s', input=verifier.script())
        self.assertIn(verifier.SUCCESS, result.stdout)
        for path in ('/root/.ssh/authorized_keys', '/home/test/.ssh/authorized_keys'):
            self.assertEqual(docker('exec', container, 'cat', path).stdout.strip(), self.other_key)
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
