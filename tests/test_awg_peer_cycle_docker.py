"""AWG peers through awg-add-user.sh and awg-del-user.sh, which start the
built agent, against a fake Docker and the real awg_profile.py. In the
Docker suite, where CI builds the agent."""
import base64
import json
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import unittest
import zlib

ASSETS = pathlib.Path(__file__).resolve().parents[1] / 'runtime_assets'
AGENT_BINARY = ASSETS.parent / 'rust/node-agent/target/debug/node-plane-agent'
PROFILE_TOOL = ASSETS / 'awg_profile.py'


def fixture(root):
    """The entry points against a fake Docker in `root`: returns the server
    profile, config, Docker log, fake Docker and environment."""
    node_env = root / 'node.env'
    config = root / 'wg0.conf'
    docker_log = root / 'docker.log'
    fake_bin = root / 'bin'
    fake_bin.mkdir()
    profile = subprocess.check_output([sys.executable, str(PROFILE_TOOL), 'init', str(config)], text=True)
    config.write_text('[Interface]\nPrivateKey = serverprivate\nAddress = 10.8.1.1/24\nListenPort = 51820\n' + profile)
    node_env.write_text(
        f'AWG_CONFIG={shlex.quote(str(config))}\n'
        f'AWG_CLIENTS_DIR={shlex.quote(str(root / "clients"))}\n'
        'SERVER_KEY=msk1\n'
        'AWG_SERVER_IP=203.0.113.10\n'
        f'AWG_PROFILE_TOOL={shlex.quote(str(PROFILE_TOOL))}\n')
    docker = fake_bin / 'docker'
    docker.write_text(
        '#!/bin/sh\n'
        f'printf \'%s\\n\' "$*" >> {shlex.quote(str(docker_log))}\n'
        'case "$1" in\n'
        '  ps) echo amnezia-awg ;;\n'
        '  exec)\n'
        '    case "$*" in\n'
        "      *show*allowed-ips*) printf 'serverkey\\t10.8.1.1/32\\n' ;;\n"
        "      *genkey*) echo 'private public preshared' ;;\n"
        '      *public-key*) echo serverpublic ;;\n'
        '    esac ;;\n'
        'esac\n')
    docker.chmod(0o755)
    for name in ('awg-add-user.sh', 'awg-del-user.sh'):
        (root / name).write_text((ASSETS / name).read_text().replace(
            'source /etc/node-plane/node.env', f'source {shlex.quote(str(node_env))}', 1))
    env = {**os.environ, 'PATH': f'{fake_bin}:{os.environ["PATH"]}', 'NODE_PLANE_AGENT_BIN': str(AGENT_BINARY)}
    return profile, config, docker_log, docker, env


def decode(key):
    """A vpn:// key as AmneziaVPN reads it."""
    data = base64.urlsafe_b64decode(key + '=' * (-len(key) % 4))
    payload = zlib.decompress(data[4:])
    assert int.from_bytes(data[:4], 'big') == len(payload)
    return json.loads(payload)


@unittest.skipUnless(os.environ.get('NODE_PLANE_TEST_DOCKER') == '1', 'requires the Docker suite')
class AwgPeerCycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.profile, self.config, self.log, self.docker, self.env = fixture(self.root)

    def run_script(self, name, profile, check=True):
        result = subprocess.run(['bash', str(self.root / name), profile], env=self.env,
                                text=True, capture_output=True)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_issue_allocates_an_address_and_exports_an_amnezia_key(self):
        result = self.run_script('awg-add-user.sh', 'alice')
        self.assertIn('Address = 10.8.1.2/32, fd42:6e70:6177:2::a08:102/128', result.stdout)
        self.assertIn('AllowedIPs = 0.0.0.0/0, ::/0', result.stdout)
        self.assertIn('AllowedIPs = 10.8.1.2/32, fd42:6e70:6177:2::a08:102/128', self.config.read_text())
        self.assertIn('preshared-key', self.log.read_text())
        self.assertNotIn('preshared ', self.log.read_text().replace('preshared-key', ''),
                         'the preshared key never reaches argv')
        for field in ('HeaderProtectionKey', 'RandomTrailers', 'I5', 'RekeyAfterTime', 'RekeyTimeout',
                      'RejectAfterTime', 'KeepaliveTimeout', 'MaxHandshakeAttempts'):
            line = next(line for line in self.profile.splitlines() if line.startswith(field + ' = '))
            self.assertIn(line, result.stdout)
        key = next(line[6:] for line in result.stdout.splitlines() if line.startswith('vpn://'))
        connection = decode(key)
        self.assertEqual(connection['description'], 'msk1 · alice')
        self.assertEqual(connection['defaultContainer'], 'amnezia-awg2')
        last = json.loads(connection['containers'][0]['awg']['last_config'])
        self.assertEqual(last['allowed_ips'], ['0.0.0.0/0', '::/0'])
        self.assertIn('fd42:6e70:6177:2::a08:102/128', last['config'])
        self.assertEqual(last['client_ip'], '10.8.1.2')

        saved = self.root / 'clients' / 'msk1-alice.txt'
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        generated = self.log.read_text().count('genkey')
        self.assertEqual(self.run_script('awg-add-user.sh', 'alice').stdout, result.stdout)
        self.assertEqual(self.log.read_text().count('genkey'), generated)

        # A peer without its saved config is replaced on the same address.
        saved.unlink()
        replaced = self.run_script('awg-add-user.sh', 'alice')
        self.assertIn('Address = 10.8.1.2/32', replaced.stdout)
        self.assertEqual(self.config.read_text().count('# msk1-alice'), 1)
        self.assertIn('exec -i amnezia-awg wg set wg0 peer public remove', self.log.read_text())

    def test_revoked_peer_is_restored_with_its_identity_and_siblings_stay(self):
        first = self.run_script('awg-add-user.sh', 'alice')
        cached = self.root / 'clients' / 'msk1-alice.txt'
        revoked = self.run_script('awg-del-user.sh', 'alice')
        self.assertEqual(revoked.stdout, 'OK\n')
        self.assertNotIn('# msk1-alice', self.config.read_text())
        self.assertIn('exec -i amnezia-awg wg set wg0 peer public remove', self.log.read_text())
        self.assertTrue(cached.exists())
        self.assertTrue((self.root / 'clients' / 'msk1-alice.revoked.json').exists())
        restored = self.run_script('awg-add-user.sh', 'alice')
        self.assertEqual(restored.stdout, first.stdout)
        self.assertEqual(self.config.read_text().count('# msk1-alice'), 1)
        self.assertNotIn('restart', self.log.read_text())

        # Another peer gets its own address and survives the revocation.
        self.docker.write_text(self.docker.read_text().replace(
            "echo 'private public preshared'", "echo 'phoneprivate phonepublic phonepreshared'"))
        phone = self.run_script('awg-add-user.sh', 'phone_peer')
        self.assertIn('PrivateKey = phoneprivate', phone.stdout)
        self.assertIn('Address = 10.8.1.3/32', phone.stdout)
        self.run_script('awg-del-user.sh', 'alice')
        self.assertNotIn('# msk1-alice', self.config.read_text())
        self.assertIn('# msk1-phone_peer', self.config.read_text())
        self.assertEqual((self.root / 'clients' / 'msk1-phone_peer.txt').read_text(), phone.stdout)

    def test_a_stopped_container_refuses_changes(self):
        self.run_script('awg-add-user.sh', 'alice')
        self.docker.write_text(self.docker.read_text().replace('ps) echo amnezia-awg', 'ps) echo other'))
        self.assertNotEqual(self.run_script('awg-del-user.sh', 'alice', check=False).returncode, 0)
        self.assertNotEqual(self.run_script('awg-add-user.sh', 'bob', check=False).returncode, 0)
        self.assertIn('# msk1-alice', self.config.read_text())
        self.assertNotIn('# msk1-bob', self.config.read_text())


if __name__ == '__main__':
    unittest.main()
