"""Traffic counters of a whole node through the built agent, on live Xray and
AWG containers: one read per protocol, unknown identities left out."""
import json
import os
import subprocess
import unittest
from uuid import uuid4

from tests import test_runtime_docker as runtime
from tests.test_runtime_docker import AGENT_BINARY, docker


@unittest.skipUnless(os.environ.get('NODE_PLANE_TEST_DOCKER') == '1', 'requires disposable Docker runtime fixtures')
@unittest.skipUnless(AGENT_BINARY.is_file(), 'requires a built node agent')
class AgentTrafficDockerTests(unittest.TestCase):
    setUp = runtime.DockerRuntimeTests.setUp
    container = runtime.DockerRuntimeTests.container
    network = runtime.DockerRuntimeTests.network
    agent_xray = runtime.DockerRuntimeTests.agent_xray
    awg_pair = runtime.DockerRuntimeTests.awg_pair
    sync = runtime.DockerRuntimeTests.sync

    def batch(self, node_env, items):
        result = subprocess.run([str(AGENT_BINARY), 'traffic-batch',
                                 json.dumps({'node_key': 'n1', 'items': items})],
                                env={**os.environ, 'NODE_PLANE_NODE_ENV': str(node_env)},
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        reply = json.loads(result.stdout)
        self.assertEqual(reply['node_key'], 'n1')
        return {(item['protocol'], item['runtime_name']): item for item in reply['items']}

    def test_one_batch_reads_every_identity_of_both_protocols(self):
        xray_config, xray, _ = self.agent_xray()
        alice, bob = str(uuid4()), str(uuid4())
        self.sync(xray_config, xray, 'add', alice, name='alice')
        self.sync(xray_config, xray, 'add', bob, name='bob')

        server, client = self.awg_pair('quic')
        docker('exec', client, 'ping', '-c', '3', '-W', '2', '10.88.0.1')
        peer = docker('exec', server, 'awg', 'show', 'awg0', 'peers').stdout.strip()
        # The agent reads the node's config from the host, as on a node.
        awg_config = self.root / 'agent-awg' / 'awg0.conf'
        awg_config.parent.mkdir()
        awg_config.write_text(f'[Interface]\nPrivateKey = secret\n\n# n1-carol\n[Peer]\nPublicKey = {peer}\n')

        node_env = self.root / 'agent' / 'node.env'
        node_env.parent.mkdir()
        node_env.write_text(f'SERVER_KEY=n1\nXRAY_CONFIG={xray_config}\nXRAY_CONTAINER_NAME={xray}\n'
                            f'AWG_CONFIG={awg_config}\nAWG_CONTAINER_NAME={server}\nAWG_IFACE=awg0\n')
        (node_env.parent / 'profile-intents.sqlite3.lock').touch()

        other_key = 'b2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s='
        items = [
            {'runtime_name': 'alice', 'protocol': 'xray', 'identity': alice},
            {'runtime_name': 'bob', 'protocol': 'xray', 'identity': bob},
            # Not this node's identity: left out, the others still count.
            {'runtime_name': 'dave', 'protocol': 'xray', 'identity': str(uuid4())},
            {'runtime_name': 'carol', 'protocol': 'awg', 'identity': peer},
            {'runtime_name': 'erin', 'protocol': 'awg', 'identity': other_key},
        ]
        found = self.batch(node_env, items)
        self.assertEqual(set(found), {('xray', 'alice'), ('xray', 'bob'), ('awg', 'carol')})
        self.assertEqual(found[('xray', 'alice')]['identity'], alice)
        # Users without traffic have no Xray counters yet: zero.
        self.assertEqual((found[('xray', 'bob')]['uplink_bytes'], found[('xray', 'bob')]['downlink_bytes']), (0, 0))
        # Xray counts each inbound separately; AWG has one.
        self.assertEqual(found[('xray', 'bob')]['inbounds'], {
            inbound: {'uplink_bytes': 0, 'downlink_bytes': 0} for inbound in ('vless-tcp', 'vless-xhttp')})
        carol = found[('awg', 'carol')]
        self.assertEqual(carol['inbounds'], {})
        self.assertGreater(carol['uplink_bytes'], 0)
        self.assertGreater(carol['downlink_bytes'], 0)
        self.assertRegex(carol['epoch'], r'^[0-9a-f]{64}$')
        self.assertNotEqual(carol['epoch'], found[('xray', 'alice')]['epoch'])

        # A restarted container is a new epoch; a stopped one is unknown.
        docker('restart', '-t', '0', xray)
        runtime.DockerRuntimeTests.wait_users(self, xray)
        restarted = self.batch(node_env, items)
        self.assertNotEqual(restarted[('xray', 'alice')]['epoch'], found[('xray', 'alice')]['epoch'])
        self.assertEqual(restarted[('awg', 'carol')]['epoch'], carol['epoch'])
        docker('stop', '-t', '0', server)
        self.assertEqual(set(self.batch(node_env, items)), {('xray', 'alice'), ('xray', 'bob')})


if __name__ == '__main__':
    unittest.main()
