"""Actual SIGKILL boundaries using PostgreSQL and the agent's durable helper.

Run separately with NODE_PLANE_TEST_POSTGRES_DSN; no production hosts or RPCs.
"""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from backend.executor import IntentExecutor
from backend.profile_commands import ProfileCommands
from backend.profiles import ProfileRepository
from db.postgres_db import PostgresDB
from tests import test_backups_postgres as fixture
from tests.test_core_reliability_postgres import HookDatabase


SPEC = importlib.util.spec_from_file_location('fault_intents',
    Path(__file__).resolve().parents[1] / 'runtime_assets/apply-profile-intent.py')
INTENTS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INTENTS)


class DurableDriver:
    def __init__(self, root, boundary='none'):
        self.root, self.boundary = Path(root), boundary

    def pause(self, boundary):
        if self.boundary == boundary:
            (self.root / 'ready').write_text(boundary)
            while True:
                time.sleep(0.05)

    @staticmethod
    def intent(task, value):
        return {'command_id': task, 'protocol': value['protocol'],
            'runtime_name': value['runtime_name'], 'revision': value['desired_revision'],
            'action': value['action'], 'uuid': '', 'short_id': ''}

    def execute(self, task, value):
        self.pause('before_rpc')
        def mutate(intent, lock_fd):
            with (self.root / 'mutations').open('a') as stream:
                stream.write(intent['command_id'] + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            self.pause('during_mutation')
            return {'summary': 'applied', 'payload_json': '{}'}
        result = INTENTS.apply(self.intent(task, value), self.root / 'agent.sqlite3', mutate)
        self.pause('after_success')
        return {'succeeded': True, 'driver_operation_id': task,
            'result_json': result['payload_json']}

    def lookup(self, task, value):
        try:
            result = INTENTS.lookup(self.intent(task, value), self.root / 'agent.sqlite3')
        except Exception:
            return {'succeeded': False}
        return {'succeeded': True, 'driver_operation_id': task,
            'result_json': result['payload_json']}


def killed_worker(dsn, root, boundary):
    driver = DurableDriver(root, boundary)
    def hook(sql):
        if sql.startswith('UPDATE backend_operation_tasks SET status = ?'):
            driver.pause('during_commit')
    db = PostgresDB(dsn)
    with open(Path(root) / 'worker.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        IntentExecutor(HookDatabase(db, hook), driver).run_one()


def rpc_server(root, mode):
    import grpc
    from driver.v1 import provisioning_service_pb2 as messages, provisioning_service_pb2_grpc
    from driver.v1 import operation_service_pb2_grpc
    root = Path(root)
    driver = DurableDriver(root)
    class Provisioning(provisioning_service_pb2_grpc.ProvisioningServiceServicer):
        @staticmethod
        def value(request):
            return {'protocol': request.protocol_kind, 'runtime_name': request.runtime_name,
                'desired_revision': request.desired_revision, 'action': request.action}

        def ApplyProfileIntent(self, request, context):
            task = dict(context.invocation_metadata())['x-node-plane-command-id']
            driver.execute(task, self.value(request))
            (root / 'rpc-applied').touch()
            # Parent kills this process after durable mutation, before any reply.
            while context.is_active():
                time.sleep(0.02)
            context.abort(grpc.StatusCode.CANCELLED, 'test interrupted')

        def RecoverProfileIntent(self, request, context):
            task = dict(context.invocation_metadata())['x-node-plane-command-id']
            result = driver.lookup(task, self.value(request))
            if not result['succeeded']:
                context.abort(grpc.StatusCode.FAILED_PRECONDITION, 'unconfirmed')
            return messages.RecoverProfileIntentResponse(payload_json=result['result_json'])

    class Operations(operation_service_pb2_grpc.OperationServiceServicer):
        def GetOperationByCommand(self, request, context):
            context.abort(grpc.StatusCode.NOT_FOUND, 'driver restarted; consult agent journal')

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    provisioning_service_pb2_grpc.add_ProvisioningServiceServicer_to_server(Provisioning(), server)
    operation_service_pb2_grpc.add_OperationServiceServicer_to_server(Operations(), server)
    port = server.add_insecure_port('127.0.0.1:0')
    server.start()
    (root / ('rpc-port-' + mode)).write_text(str(port))
    server.wait_for_termination()


@unittest.skipUnless(os.environ.get('NODE_PLANE_TEST_POSTGRES_DSN'), 'requires disposable PostgreSQL')
class ProcessFaultPostgresTests(unittest.TestCase):
    setUp = fixture.BackupsPostgresTests.setUp
    drop_schema = fixture.BackupsPostgresTests.drop_schema

    def scenario(self, boundary, confirmed):
        profile = ProfileRepository(self.db).create_profile(runtime_name='kill_test', display_name='Kill test')
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_nodes(key,title,region,protocols_json) VALUES ('node','Test','Europe','[\"awg\"]')")
        ProfileCommands(self.db).execute(self.actor, str(uuid4()), action='grants',
            profile_id=profile, revision=1, values={'grants': [{'node_key': 'node', 'protocol': 'awg'}]})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = subprocess.Popen([sys.executable, '-m', 'tests.test_process_faults_postgres',
                '--worker', self.db.dsn, directory, boundary], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 10
                while not (root / 'ready').exists() and time.monotonic() < deadline and process.poll() is None:
                    time.sleep(0.02)
                self.assertTrue((root / 'ready').exists(), process.poll())
                process.kill()
                process.communicate(timeout=5)
                self.assertEqual(process.returncode, -9)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
            # OS death releases the worker flock and the helper's inherited lock.
            with (root / 'worker.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            worker = IntentExecutor(self.db, DurableDriver(root))
            worker.recover()
            self.assertEqual(worker.reconcile_completed(), int(confirmed))
            with self.db.connect() as conn:
                task = conn.execute('SELECT * FROM backend_operation_tasks').fetchone()
            self.assertEqual(task['status'], 'succeeded' if confirmed else 'blocked')
            self.assertFalse(worker.run_one())
            calls = (root / 'mutations').read_text().splitlines() if (root / 'mutations').exists() else []
            self.assertEqual(len(calls), 0 if boundary == 'before_rpc' else 1)
            if boundary == 'during_mutation':
                # A restarted helper refuses an uncertain command instead of replaying it.
                with self.assertRaisesRegex(ValueError, 'did not complete'):
                    DurableDriver(root).execute(task['id'], json.loads(task['intent_json']))
                self.assertEqual((root / 'mutations').read_text().splitlines(), calls)

    def test_sigkill_before_dispatch_remains_blocked(self):
        self.scenario('before_rpc', False)

    def test_sigkill_during_agent_mutation_never_replays(self):
        self.scenario('during_mutation', False)

    def test_sigkill_after_agent_journal_commit_recovers_success(self):
        self.scenario('after_success', True)

    def test_sigkill_inside_result_transaction_rolls_back_then_recovers(self):
        self.scenario('during_commit', True)

    def test_dropped_grpc_reply_blocks_until_restarted_agent_journal_confirms(self):
        import grpc
        from backend.driver_transport import GrpcIntentDriver
        profile = ProfileRepository(self.db).create_profile(runtime_name='rpc_test', display_name='RPC test')
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_nodes(key,title,region,protocols_json) VALUES ('node','Test','Europe','[\"awg\"]')")
        ProfileCommands(self.db).execute(self.actor, str(uuid4()), action='grants', profile_id=profile,
            revision=1, values={'grants': [{'node_key': 'node', 'protocol': 'awg'}]})
        with tempfile.TemporaryDirectory() as directory:
            root, processes = Path(directory), []
            def wait_file(name):
                deadline = time.monotonic() + 10
                while not (root / name).exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue((root / name).exists(), name)
            def start(mode):
                process = subprocess.Popen([sys.executable, '-m', 'tests.test_process_faults_postgres',
                    '--rpc-server', directory, mode], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                processes.append(process)
                wait_file('rpc-port-' + mode)
                return process, (root / ('rpc-port-' + mode)).read_text()
            try:
                server, port = start('initial')
                with grpc.insecure_channel('127.0.0.1:' + port) as channel:
                    driver = GrpcIntentDriver(channel, timeout=3)
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        request = pool.submit(IntentExecutor(self.db, driver).run_one)
                        wait_file('rpc-applied')
                        server.kill()
                        server.communicate(timeout=5)
                        self.assertTrue(request.result(timeout=5))
                    self.assertEqual(IntentExecutor(self.db, driver).reconcile_completed(), 0)
                with self.db.connect() as conn:
                    self.assertEqual(conn.execute('SELECT status FROM backend_operation_tasks').fetchone()['status'], 'blocked')
                restarted, port = start('recovery')
                with grpc.insecure_channel('127.0.0.1:' + port) as channel:
                    worker = IntentExecutor(self.db, GrpcIntentDriver(channel, timeout=3))
                    self.assertEqual(worker.reconcile_completed(), 1)
                    self.assertFalse(worker.run_one())
                self.assertEqual(len((root / 'mutations').read_text().splitlines()), 1)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=5)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--worker':
        killed_worker(*sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == '--rpc-server':
        rpc_server(*sys.argv[2:])
    else:
        unittest.main()
