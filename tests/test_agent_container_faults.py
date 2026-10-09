"""Opt-in SIGKILL tests of the real Rust agent, mTLS and production journal.

Only disposable Docker containers are used. A controlled mutation script marks
the interruption boundary; it does not claim to exercise a live VPN tunnel.
Requires a current debug agent binary and the cached release-builder image.
"""
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import select
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from uuid import uuid4

import grpc

REPO = Path(__file__).resolve().parents[1]


def docker(*args, check=True):
    return subprocess.run(['docker', *args], check=check, capture_output=True,
                          text=True, timeout=30)


class LocalTLSBridge:
    """Loopback test transport: gRPC framing stays unchanged, OpenSSL owns TLS.

    The pinned grpcio client's TLS handshake is incompatible with this local
    Rust debug server. This avoids changing production dependencies or disabling
    agent mTLS. Every forwarded connection still validates the server certificate.
    """
    def __init__(self, port, root, client_certificate=True):
        self.remote = int(port)
        self.context = ssl.create_default_context(cafile=str(root/'ca.crt'))
        self.context.set_alpn_protocols(['h2'])
        if client_certificate:
            self.context.load_cert_chain(root/'client.crt',root/'client.key')
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1',0))
        self.listener.listen()
        self.listener.settimeout(.2)
        self.port = self.listener.getsockname()[1]
        self.stopped = threading.Event()
        self.connections = []
        self.worker = threading.Thread(target=self.accept,daemon=True)
        self.worker.start()

    def accept(self):
        while not self.stopped.is_set():
            try: client,_ = self.listener.accept()
            except socket.timeout: continue
            except OSError: return
            threading.Thread(target=self.forward,args=(client,),daemon=True).start()

    def forward(self,client):
        remote = None
        self.connections.append(client)
        try:
            connection = socket.create_connection(('127.0.0.1',self.remote),timeout=2)
            remote = self.context.wrap_socket(connection,server_hostname='localhost')
            self.connections.append(remote)
            while not self.stopped.is_set():
                ready,_,_ = select.select([client,remote],[],[],.2)
                if remote.pending() and remote not in ready: ready.append(remote)
                for source in ready:
                    value = source.recv(65536)
                    if not value: return
                    (remote if source is client else client).sendall(value)
        except (OSError,ssl.SSLError,ValueError):
            pass
        finally:
            client.close()
            if remote is not None: remote.close()

    def close(self):
        self.stopped.set()
        self.listener.close()
        for connection in self.connections:
            try: connection.close()
            except OSError: pass
        self.worker.join(timeout=2)


@unittest.skipUnless(os.environ.get('NODE_PLANE_TEST_DOCKER') == '1',
                     'requires disposable Docker agent fixtures')
class AgentContainerFaultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='node-plane-agent-fault-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        binary = REPO / 'rust/node-agent/target/debug/node-plane-agent'
        if not binary.is_file():
            raise RuntimeError('Build the current Rust agent first')
        shutil.copy(binary, cls.root / 'agent-bin')
        # The debug binary uses host glibc. Copy only its runtime libraries into
        # the isolated container; do not mount host directories or Docker socket.
        libs = cls.root / 'libs'
        libs.mkdir()
        dependencies = subprocess.run(['ldd',str(binary)],check=True,
                                      capture_output=True,text=True).stdout
        for source in re.findall(r'(/[^\s()]+)',dependencies):
            path = Path(source)
            shutil.copy(path.resolve(),libs/path.name)
        generated = cls.root / 'generated'
        generated.mkdir()
        subprocess.run([sys.executable, '-m', 'grpc_tools.protoc',
                        '-I'+str(REPO/'proto'), '--python_out='+str(generated),
                        '--grpc_python_out='+str(generated),
                        str(REPO/'proto/agent/v1/types.proto'),
                        str(REPO/'proto/agent/v1/agent_service.proto')], check=True)
        sys.path.insert(0, str(generated))
        cls.addClassCleanup(lambda: sys.path.remove(str(generated)))
        from agent.v1 import agent_service_pb2, agent_service_pb2_grpc, types_pb2
        cls.pb, cls.rpc = agent_service_pb2, agent_service_pb2_grpc
        cls.empty = types_pb2.AgentEmpty
        def openssl(*args):
            subprocess.run(['openssl', *args], cwd=cls.root, check=True,
                           capture_output=True, timeout=10)
        openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                '-subj', '/CN=Fault test CA', '-keyout', 'ca.key', '-out', 'ca.crt')
        for identity in ('server', 'client'):
            openssl('req', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN='+identity,
                    '-keyout', identity+'.key', '-out', identity+'.csr')
            extension = cls.root / (identity+'.ext')
            extension.write_text('basicConstraints=critical,CA:FALSE\n'
                                 'keyUsage=critical,digitalSignature,keyEncipherment\n'
                                 'subjectAltName=DNS:localhost\nextendedKeyUsage='+
                                 ('serverAuth' if identity=='server' else 'clientAuth')+'\n')
            openssl('x509', '-req', '-in', identity+'.csr', '-CA', 'ca.crt',
                    '-CAkey', 'ca.key', '-CAcreateserial', '-days', '1',
                    '-extfile', str(extension), '-out', identity+'.crt')
        shutil.copy(REPO/'runtime_assets/apply-profile-intent.py', cls.root/'production.py')
        (cls.root/'apply-profile-intent.py').write_text('''#!/usr/bin/python3
import json, pathlib, subprocess, sys, time
root = pathlib.Path('/test')
result = subprocess.run(['/usr/bin/python3', '/test/production.py', *sys.argv[1:]],
                        capture_output=True)
if result.returncode == 0 and (root/'mode').read_text() == 'after_commit' and sys.argv[1].startswith('{'):
    (root/'ready').touch()
    while not (root/'release').exists(): time.sleep(.02)
sys.stdout.buffer.write(result.stdout)
sys.stderr.buffer.write(result.stderr)
sys.exit(result.returncode)
''')
        (cls.root/'xray-add-user-existing.sh').write_text('''#!/usr/bin/python3
import os, pathlib, time
root = pathlib.Path('/test')
mode = (root/'mode').read_text()
def pause():
    (root/'ready').touch()
    while not (root/'release').exists(): time.sleep(.02)
if mode == 'before_mutation': pause()
with (root/'counter').open('a') as stream:
    stream.write('mutation\\n'); stream.flush(); os.fsync(stream.fileno())
if mode == 'during_mutation': pause()
print('OK')
''')
        for name in ('apply-profile-intent.py', 'xray-add-user-existing.sh'):
            (cls.root/name).chmod(0o755)
        (cls.root/'agent.toml').write_text('''node_key = "fault-node"
listen_addr = "0.0.0.0:50061"
heartbeat_seconds = 1
runtime_root = "/test"
state_dir = "/var/lib/node-plane-agent"
log_dir = "/var/log/node-plane-agent"
node_env_path = "/etc/node-plane/node.env"
xray_config_path = "/test/config.json"
awg_config_path = "/test/wg0.conf"
tls_certificate_path = "/test/server.crt"
tls_key_path = "/test/server.key"
tls_client_ca_path = "/test/ca.crt"
''')

    def setUp(self):
        self.name = 'np-test-agent-fault-'+uuid4().hex[:12]
        identity = docker('create', '--name', self.name, '-p', '127.0.0.1::50061',
                          '--entrypoint', 'sh', 'node-plane-release-builder:bookworm',
                          '-c', 'sleep 86400').stdout.strip()
        self.addCleanup(lambda: docker('rm', '-f', identity, check=False))
        docker('cp', str(self.root)+'/.', self.name+':/test')
        docker('start', self.name)
        docker('exec', self.name, 'sh', '-c',
               'mkdir -p /etc/node-plane; printf none > /test/mode')
        self.port = docker('port', self.name, '50061/tcp').stdout.strip().rsplit(':',1)[1]
        self.bridge = LocalTLSBridge(self.port,self.root)
        self.addCleanup(self.bridge.close)
        self.channel = grpc.insecure_channel('127.0.0.1:'+str(self.bridge.port),
                                            options=(('grpc.enable_http_proxy',0),))
        self.addCleanup(self.channel.close)
        self.stub = self.rpc.NodeAgentServiceStub(self.channel)
        self.start_agent()
        self.request = self.pb.ApplyProfileIntentRequest(command_id=str(uuid4()),
            protocol_kind='xray', profile_name='fault_test', desired_revision=1,
            action='ensure', uuid=str(uuid4()), short_id='0123456789abcdef')

    def start_agent(self):
        self.port = docker('port', self.name, '50061/tcp').stdout.strip().rsplit(':',1)[1]
        self.bridge.remote = int(self.port)
        docker('exec', '-d', self.name, 'sh', '-c',
               'echo $$ > /test/agent.pid; export NODE_AGENT_CONFIG_PATH=/test/agent.toml; '
               'exec /test/libs/ld-linux-x86-64.so.2 --library-path /test/libs '
               '/test/agent-bin > /test/agent.log 2>&1')
        deadline = time.monotonic()+12
        while True:
            try:
                self.stub.GetRuntimeFacts(self.empty(), timeout=.5)
                return
            except grpc.RpcError:
                if time.monotonic() >= deadline:
                    probe = subprocess.run(['openssl','s_client','-connect','127.0.0.1:'+self.port,
                        '-servername','localhost','-alpn','h2','-CAfile',str(self.root/'ca.crt'),
                        '-cert',str(self.root/'client.crt'),'-key',str(self.root/'client.key')],
                        input='',capture_output=True,text=True,timeout=3)
                    self.fail(docker('exec',self.name,'cat','/test/agent.log').stdout+
                              probe.stderr)
                time.sleep(.05)

    def mode(self, value):
        docker('exec',self.name,'sh','-c','printf "%s" "$1" > /test/mode', 'mode',value)

    def wait_ready(self):
        deadline = time.monotonic()+10
        while docker('exec',self.name,'test','-e','/test/ready',check=False).returncode:
            if time.monotonic() >= deadline: self.fail('Interruption boundary not reached')
            time.sleep(.03)

    def journal_status(self):
        return docker('exec',self.name,'python3','-c',
                      "import sqlite3; print(sqlite3.connect('/etc/node-plane/profile-intents.sqlite3').execute('SELECT status FROM commands').fetchone()[0])").stdout.strip()

    def count(self):
        return docker('exec',self.name,'sh','-c',
                      'if [ -f /test/counter ]; then wc -l < /test/counter; else echo 0; fi').stdout.strip()

    def test_agent_sigkill_before_mutation_orphan_completes_once_and_recovers(self):
        self.mode('before_mutation')
        call = self.stub.ApplyProfileIntent.future(self.request, timeout=15)
        self.wait_ready()
        self.assertEqual(self.count(),'0')
        docker('exec',self.name,'sh','-c','kill -9 "$(cat /test/agent.pid)"; touch /test/release')
        with self.assertRaises(grpc.RpcError): call.result(timeout=5)
        self.start_agent()
        self.stub.RecoverProfileIntent(self.request,timeout=5)
        self.assertEqual(self.journal_status(),'succeeded')
        self.assertEqual(self.count(),'1')

    def test_container_sigkill_during_mutation_blocks_replay_after_restart(self):
        self.mode('during_mutation')
        call = self.stub.ApplyProfileIntent.future(self.request,timeout=15)
        self.wait_ready()
        self.assertEqual(self.count(),'1')
        docker('kill',self.name)
        with self.assertRaises(grpc.RpcError): call.result(timeout=5)
        docker('start',self.name)
        self.mode('none')
        self.start_agent()
        for method in (self.stub.RecoverProfileIntent,self.stub.ApplyProfileIntent):
            with self.assertRaises(grpc.RpcError) as error: method(self.request,timeout=5)
            self.assertEqual(error.exception.code(),grpc.StatusCode.FAILED_PRECONDITION)
        self.assertEqual(self.journal_status(),'running')
        self.assertEqual(self.count(),'1')

    def test_lost_reply_after_commit_recovers_without_another_mutation(self):
        self.mode('after_commit')
        call = self.stub.ApplyProfileIntent.future(self.request,timeout=15)
        self.wait_ready()
        self.assertEqual(self.journal_status(),'succeeded')
        docker('kill',self.name)
        with self.assertRaises(grpc.RpcError): call.result(timeout=5)
        docker('start',self.name)
        self.mode('none')
        self.start_agent()
        self.stub.RecoverProfileIntent(self.request,timeout=5)
        self.stub.ApplyProfileIntent(self.request,timeout=5)
        self.assertEqual(self.count(),'1')
        conflict = self.pb.ApplyProfileIntentRequest()
        conflict.CopyFrom(self.request)
        conflict.uuid = str(uuid4())
        with self.assertRaises(grpc.RpcError) as error:
            self.stub.ApplyProfileIntent(conflict,timeout=5)
        self.assertEqual(error.exception.code(),grpc.StatusCode.FAILED_PRECONDITION)
        self.assertEqual(self.count(),'1')

    def test_missing_client_certificate_cannot_mutate(self):
        bridge = LocalTLSBridge(self.port,self.root,client_certificate=False)
        self.addCleanup(bridge.close)
        with grpc.insecure_channel('127.0.0.1:'+str(bridge.port),
                    options=(('grpc.enable_http_proxy',0),)) as channel:
            stub = self.rpc.NodeAgentServiceStub(channel)
            with self.assertRaises(grpc.RpcError): stub.ApplyProfileIntent(self.request,timeout=2)
        self.assertEqual(self.count(),'0')
        self.stub.GetRuntimeFacts(self.empty(),timeout=2)
