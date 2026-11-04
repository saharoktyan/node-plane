"""Token enrollment end to end: the built agent in a disposable container
waits for the controller, the built driver on the host enrolls it, and then
reaches it over mutual TLS with the key the agent generated itself."""
import hashlib
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from uuid import uuid4

import grpc

from backend.driver_transport import GrpcIntentDriver, _LocalTokenInterceptor

REPO = Path(__file__).resolve().parents[1]
AGENT = REPO / 'rust/node-agent/target/debug/node-plane-agent'
DRIVER = REPO / 'rust/node-driver/target/debug/node-plane-driver'
CONTROLLER = '22222222-2222-4222-8222-222222222222'


def docker(*args, check=True):
    return subprocess.run(['docker', *args], check=check, capture_output=True, text=True, timeout=30)


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


@unittest.skipUnless(os.environ.get('NODE_PLANE_TEST_DOCKER') == '1', 'requires disposable Docker agent fixtures')
class AgentEnrollmentDockerTests(unittest.TestCase):
    def setUp(self):
        for binary in (AGENT, DRIVER):
            if not binary.is_file():
                raise RuntimeError(f'Build {binary.name} first')
        temp = tempfile.TemporaryDirectory(prefix='node-plane-enrollment-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        node = self.root / 'node'
        (node / 'tls').mkdir(parents=True)
        shutil.copy(AGENT, node / 'agent-bin')
        # The debug binary uses host glibc: copy only its libraries.
        (node / 'libs').mkdir()
        for source in re.findall(r'(/[^\s()]+)', subprocess.run(
                ['ldd', str(AGENT)], check=True, capture_output=True, text=True).stdout):
            shutil.copy(Path(source).resolve(), node / 'libs' / Path(source).name)
        # The release archive, served to the installer from the node's disk.
        archive = self.root / 'archive'
        archive.mkdir()
        shutil.copy(AGENT, archive / 'node-plane-agent-linux-amd64')
        subprocess.run(['tar', '-C', str(archive), '-czf', str(node / 'agent.tar.gz'),
                        'node-plane-agent-linux-amd64'], check=True)
        self.agent_sha256 = hashlib.sha256((node / 'agent.tar.gz').read_bytes()).hexdigest()
        shutil.copy(REPO / 'scripts/install_agent.sh', node / 'install_agent.sh')
        # The node's systemd and an active ufw record what they are asked.
        (node / 'bin').mkdir()
        (node / 'bin/systemctl').write_text('#!/bin/sh\necho "$*" >> /test/systemctl.log\n')
        (node / 'bin/ufw').write_text('#!/bin/sh\nif [ "$1" = status ]; then echo "Status: active"; exit 0; fi\n'
                                      'echo "$*" >> /test/ufw.log\n')
        # Speed-limit tools are present: no package installation.
        for tool in ('tc', 'nft', 'nsenter'):
            (node / 'bin' / tool).write_text('#!/bin/sh\n')
        self.token = f'{int(time.time()) + 600}.{secrets.token_hex(32)}'

        controller = self.root / 'controller'
        controller.mkdir()

        def openssl(*args):
            subprocess.run(['openssl', *args], cwd=controller, check=True, capture_output=True, timeout=10)
        openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=Enrollment test CA',
                '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign',
                '-keyout', 'ca.key', '-out', 'ca.crt')
        (controller / 'client.ext').write_text('basicConstraints=critical,CA:FALSE\n'
                                               'keyUsage=critical,digitalSignature,keyEncipherment\n'
                                               'extendedKeyUsage=clientAuth\n')
        openssl('req', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=node-plane-driver',
                '-keyout', 'client.key', '-out', 'client.csr')
        openssl('x509', '-req', '-in', 'client.csr', '-CA', 'ca.crt', '-CAkey', 'ca.key', '-set_serial', '1',
                '-days', '1', '-extfile', 'client.ext', '-out', 'client.crt')
        self.registry = controller / 'enrolled-agents'

        self.name = 'np-test-enroll-' + uuid4().hex[:12]
        identity = docker('create', '--name', self.name, '-p', '127.0.0.1::50061', '--entrypoint', 'sh',
                          'node-plane-release-builder:bookworm', '-c', 'sleep 86400').stdout.strip()
        self.addCleanup(lambda: docker('rm', '-f', identity, check=False))
        docker('cp', f'{node}/.', f'{self.name}:/test')
        docker('start', self.name)
        docker('exec', self.name, 'sh', '-c', 'for tool in /test/bin/*; do install -m 755 "$tool" /usr/local/bin/; done; '
               'mkdir -p /etc/systemd/system')
        self.target = '127.0.0.1:' + docker('port', self.name, '50061/tcp').stdout.strip().rsplit(':', 1)[1]

        shared = self.root / 'shared'
        (shared / 'data').mkdir(parents=True)
        self.driver_address = f'127.0.0.1:{free_port()}'
        self.driver_env = {
            'PATH': os.environ['PATH'], 'NODE_DRIVER_LISTEN_ADDR': self.driver_address,
            'NODE_PLANE_DRIVER_LOCAL_TOKEN': 'local-test-token', 'NODE_PLANE_SHARED_DIR': str(shared),
            'NODE_AGENT_CA_CERT': str(controller / 'ca.crt'),
            'NODE_AGENT_CLIENT_CERT': str(controller / 'client.crt'),
            'NODE_AGENT_CLIENT_KEY': str(controller / 'client.key'),
            # Never the host's own agent.
            'NODE_AGENT_CONFIG_PATH': str(self.root / 'absent.toml'), 'NODE_AGENT_TARGETS': '',
        }
        self.driver = self.start_driver()

    def install(self, token=None):
        return docker('exec', self.name, 'sh', '/test/install_agent.sh', '--node-key', 'n1',
                      '--token', token or self.token, '--release', 'v0.0.0-test',
                      '--agent-sha256', self.agent_sha256, '--agent-url', 'file:///test/agent.tar.gz', check=False)

    def start_agent(self, previous_node=None):
        if previous_node:
            # The server was node `previous_node` of this controller.
            docker('exec', self.name, 'sh', '-c',
                   f'mkdir -p /var/lib/node-plane-agent /etc/node-plane && echo {CONTROLLER} > '
                   f'/var/lib/node-plane-agent/controller-installation-id && '
                   f'echo \'node_key = "{previous_node}"\' > /etc/node-plane/agent.toml')
        installed = self.install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.assertIn('waits for the controller on TCP port 50061', installed.stdout)
        docker('exec', '-d', self.name, 'sh', '-c',
               'export NODE_AGENT_CONFIG_PATH=/etc/node-plane/agent.toml; '
               'exec /test/libs/ld-linux-x86-64.so.2 --library-path /test/libs /usr/local/bin/node-plane-agent '
               '> /test/agent.log 2>&1')

    def start_driver(self):
        self.process = process = subprocess.Popen([str(DRIVER)], env=self.driver_env,
                                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (process.kill(), process.wait()))
        channel = grpc.intercept_channel(grpc.insecure_channel(self.driver_address),
                                         _LocalTokenInterceptor('local-test-token'))
        self.addCleanup(channel.close)
        grpc.channel_ready_future(channel).result(timeout=10)
        return GrpcIntentDriver(channel, timeout=30)

    def agent_log(self):
        return docker('exec', self.name, 'cat', '/test/agent.log', check=False).stdout

    def enroll(self, token):
        deadline = time.monotonic() + 10
        while True:
            try:
                return self.driver.enroll_agent('n1', self.target, token, CONTROLLER)
            except grpc.RpcError as error:
                if error.code() != grpc.StatusCode.UNAVAILABLE or time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)

    def test_the_agent_of_another_active_node_is_not_taken_over(self):
        self.start_agent(previous_node='n2')
        with self.assertRaises(grpc.RpcError) as taken:
            self.enroll(self.token)
        self.assertEqual(taken.exception.code(), grpc.StatusCode.FAILED_PRECONDITION, self.agent_log())
        self.assertIn('n2', taken.exception.details())
        self.assertFalse(self.registry.exists())
        # Once the controller retired n2 for good, the agent is handed over.
        result = self.driver.enroll_agent('n1', self.target, self.token, CONTROLLER, ['n2'],
                                          restrict_to_controller=True)
        self.assertEqual(result['journal_archive'], '')
        # A controller with a static address keeps the port for itself.
        peer = re.fullmatch(r'agent port 50061 allowed from (\S+) only', result['firewall'])
        self.assertIsNotNone(peer, result)
        ufw = docker('exec', self.name, 'cat', '/test/ufw.log').stdout.splitlines()
        self.assertEqual(ufw, ["allow 50061/tcp comment Node Plane agent enrollment",
                               f"allow proto tcp from {peer[1]} to any port 50061 comment Node Plane agent",
                               "delete allow 50061/tcp"])
        self.assertEqual(self.registry.read_text(), f'n1={self.target}\n')

    def test_the_installer_refuses_a_tampered_agent(self):
        docker('exec', self.name, 'sh', '-c', 'echo tampered >> /test/agent.tar.gz')
        installed = self.install()
        self.assertEqual(installed.returncode, 1)
        self.assertIn("does not match the controller's checksum", installed.stderr)
        self.assertNotEqual(docker('exec', self.name, 'test', '-e', '/usr/local/bin/node-plane-agent',
                                   check=False).returncode, 0)
        self.assertNotEqual(self.install(token='not-a-token').returncode, 0)

    def test_the_driver_enrolls_a_token_agent_and_then_reaches_it_over_mutual_tls(self):
        self.start_agent()
        # Before enrollment the driver does not know the node.
        with self.assertRaises(grpc.RpcError) as unknown:
            self.driver.inspect_node('n1')
        self.assertEqual(unknown.exception.code(), grpc.StatusCode.FAILED_PRECONDITION)

        # Another installation's token does not enroll the agent.
        with self.assertRaises(grpc.RpcError) as wrong:
            self.enroll(f'{int(time.time()) + 600}.{secrets.token_hex(32)}')
        self.assertEqual(wrong.exception.code(), grpc.StatusCode.PERMISSION_DENIED, self.agent_log())
        self.assertFalse(self.registry.exists())

        result = self.enroll(self.token)
        self.assertEqual(result['journal_archive'], '')
        # The installer opened the port for enrollment; by default the agent
        # keeps it open under its own rule and mutual TLS guards it.
        self.assertEqual(result['firewall'], 'agent port 50061 open; mutual TLS guards it')
        ufw = docker('exec', self.name, 'cat', '/test/ufw.log').stdout.splitlines()
        self.assertEqual(ufw, ["allow 50061/tcp comment Node Plane agent enrollment",
                               "delete allow 50061/tcp", "allow 50061/tcp comment Node Plane agent"])
        self.assertIn('restart node-plane-agent', docker('exec', self.name, 'cat', '/test/systemctl.log').stdout)
        hardening = docker('exec', self.name, 'cat',
                           '/etc/systemd/system/node-plane-agent.service.d/hardening.conf').stdout
        self.assertIn('ProtectHome=read-only', hardening)
        self.assertEqual(self.registry.read_text(), f'n1={self.target}\n')
        files = docker('exec', self.name, 'sh', '-c',
                       'ls /etc/node-plane/tls; stat -c %a /etc/node-plane/tls /etc/node-plane/tls/server.key'
                       ).stdout.split()
        self.assertEqual(files, ['ca.crt', 'server.crt', 'server.key', '700', '600'])

        def inspected():
            deadline = time.monotonic() + 10
            while True:
                try:
                    return self.driver.inspect_node('n1')
                except grpc.RpcError:
                    if time.monotonic() >= deadline:
                        raise AssertionError(self.agent_log())
                    time.sleep(0.1)
        self.assertEqual(inspected()['node_key'], 'n1')

        # The token is spent, and a restarted driver still knows the agent.
        with self.assertRaises(grpc.RpcError):
            self.driver.enroll_agent('n1', self.target, self.token, CONTROLLER)
        self.process.terminate()
        self.process.wait(timeout=10)
        self.driver_env['NODE_DRIVER_LISTEN_ADDR'] = self.driver_address = f'127.0.0.1:{free_port()}'
        self.driver = self.start_driver()
        self.assertEqual(inspected()['node_key'], 'n1')


if __name__ == '__main__':
    unittest.main()
