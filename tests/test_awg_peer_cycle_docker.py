"""AWG peer revocation through the node agent and restoration through
awg-add-user.sh, against a fake Docker. In the Docker suite, where CI
builds the agent."""
import os
import pathlib
import shlex
import subprocess
import tempfile
import unittest

from tests.test_awg_add_user_script import SCRIPT, fixture

AGENT_BINARY = SCRIPT.parents[1] / 'rust/node-agent/target/debug/node-plane-agent'


@unittest.skipUnless(os.environ.get('NODE_PLANE_TEST_DOCKER') == '1', 'requires the Docker suite')
class AwgPeerCycleTests(unittest.TestCase):
    def test_revoked_peer_is_restored_with_its_identity_and_siblings_stay(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            _, config, docker_log, docker, node_env, runnable, env = fixture(root)
            env['NODE_PLANE_AGENT_BIN'] = str(AGENT_BINARY)
            first = subprocess.run(['bash', str(runnable), 'alice'], env=env, text=True,
                                   capture_output=True, check=True)
            cached = root / 'clients' / 'msk1-alice.txt'
            delete = root / 'awg-del-user.sh'
            delete.write_text((SCRIPT.parent / 'awg-del-user.sh').read_text().replace(
                'source /etc/node-plane/node.env', f'source {shlex.quote(str(node_env))}', 1))
            revoked = subprocess.run(['bash', str(delete), 'alice'], env=env, text=True, capture_output=True)
            self.assertEqual(revoked.returncode, 0, revoked.stderr)
            self.assertEqual(revoked.stdout, 'OK\n')
            self.assertNotIn('# msk1-alice', config.read_text())
            self.assertIn('exec -i amnezia-awg wg set wg0 peer public remove', docker_log.read_text())
            self.assertTrue(cached.exists())
            self.assertTrue((root / 'clients' / 'msk1-alice.revoked.json').exists())
            restored = subprocess.run(['bash', str(runnable), 'alice'], env=env, text=True,
                                      capture_output=True, check=True)
            self.assertEqual(restored.stdout, first.stdout)
            self.assertEqual(config.read_text().count('# msk1-alice'), 1)
            self.assertNotIn('restart amnezia-awg', docker_log.read_text())

            # Another peer gets its own address and survives the revocation.
            docker.write_text(docker.read_text().replace(
                "echo 'private public preshared'", "echo 'phoneprivate phonepublic phonepreshared'"))
            phone = subprocess.run(['bash', str(runnable), 'phone_peer'], env=env,
                                   text=True, capture_output=True, check=True)
            self.assertIn('PrivateKey = phoneprivate', phone.stdout)
            self.assertIn('Address = 10.8.1.3/32', phone.stdout)
            subprocess.run(['bash', str(delete), 'alice'], env=env, text=True, capture_output=True, check=True)
            self.assertNotIn('# msk1-alice', config.read_text())
            self.assertIn('# msk1-phone_peer', config.read_text())
            self.assertEqual((root / 'clients' / 'msk1-phone_peer.txt').read_text(), phone.stdout)

    def test_a_stopped_container_refuses_revocation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            _, config, _, docker, node_env, runnable, env = fixture(root)
            env['NODE_PLANE_AGENT_BIN'] = str(AGENT_BINARY)
            subprocess.run(['bash', str(runnable), 'alice'], env=env, text=True, capture_output=True, check=True)
            docker.write_text(docker.read_text().replace('ps) echo amnezia-awg', 'ps) echo other'))
            delete = root / 'awg-del-user.sh'
            delete.write_text((SCRIPT.parent / 'awg-del-user.sh').read_text().replace(
                'source /etc/node-plane/node.env', f'source {shlex.quote(str(node_env))}', 1))
            result = subprocess.run(['bash', str(delete), 'alice'], env=env, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('# msk1-alice', config.read_text())


if __name__ == '__main__':
    unittest.main()
