from types import SimpleNamespace
import unittest

from tests import test_backend_node_jobs
from tests.test_backend_executor import FakeDriver
from backend.executor import IntentExecutor
from backend.profiles import ProfileRepository
from backend.node_removal import NodeRemovalService
from backend.authorization import AccessDenied
from backend.removal_inventory import inventory_digest

RESOURCES = {'paths': ['/opt/node-plane-runtime'], 'containers': ['xray', 'amnezia-awg']}


class BackendNodeRemovalTests(unittest.TestCase):
    setUp = test_backend_node_jobs.BackendNodeJobTests.setUp

    def verifier(self, target, final=False):
        return SimpleNamespace(local=True, ssh_target=None,
            capture_identity=lambda key: 'a' * 64,
            capture_resources=lambda key, expected: RESOURCES,
            verify=lambda key, expected, resources: {'result': 'agent_and_standard_artifacts_absent',
                'inventory_digest': inventory_digest(resources),
                'method': 'local', 'target': 'local', 'host_fingerprint': expected,
                'checked_at': '2026-09-30T12:00:00+00:00'})

    def test_worker_completes_all_phases_without_telegram_staying_open(self):
        phases = []
        driver = SimpleNamespace(decommission=lambda key, command, phase: phases.append(phase))
        worker = NodeRemovalService(self.db, driver, self.verifier)
        worker.request(self.actor, 'n1')
        for _ in range(5):
            self.assertTrue(worker.run_one())
        self.assertEqual(phases, ['prepare', 'delete_runtime', 'uninstall'])
        self.assertEqual(worker.get(self.actor, 'n1')['status'], 'removed')
        self.assertIsNone(self.db.connection.execute('SELECT * FROM backend_nodes WHERE key=?', ('n1',)).fetchone())
        self.assertFalse(worker.run_one())

    def test_removal_waits_for_revocations_before_runtime_cleanup(self):
        profile = ProfileRepository(self.db).create_profile(runtime_name='alice', display_name='Alice')
        self.db.connection.execute('INSERT INTO backend_grants VALUES (?, ?, ?)', (profile, 'n1', 'awg'))
        phases = []
        worker = NodeRemovalService(self.db,
            SimpleNamespace(decommission=lambda key, command, phase: phases.append(phase)), self.verifier)
        worker.request(self.actor, 'n1')
        self.assertTrue(worker.run_one())
        self.assertFalse(worker.run_one())
        self.assertEqual(phases, [])
        executor = IntentExecutor(self.db, FakeDriver())
        while executor.run_one():
            pass
        self.assertTrue(worker.run_one())
        self.assertEqual(phases, ['prepare'])

    def test_verification_credentials_checked_before_any_destruction(self):
        def unavailable(*args):
            raise AccessDenied('independent_verification_key_required', 503)
        worker = NodeRemovalService(self.db, verifier_factory=unavailable)
        worker.request(self.actor, 'n1')
        self.assertFalse(worker.run_one())
        result = worker.get(self.actor, 'n1')
        self.assertEqual(result['error_code'], 'independent_verification_key_required')
        self.assertTrue(self.db.connection.execute('SELECT enabled FROM backend_nodes WHERE key=?', ('n1',)).fetchone()[0])
        self.assertEqual(worker.request(self.actor, 'n1')['removal_status'], 'blocked')
        self.assertEqual(worker.request(self.actor, 'n1', retry=True)['removal_status'], 'queued')

    def test_unavailable_inventory_prevents_drain_and_cleanup(self):
        phases = []
        def unavailable_inventory(target, final=False):
            verifier = self.verifier(target, final)
            def fail(*args):
                raise ValueError('inventory unavailable')
            verifier.capture_resources = fail
            return verifier
        worker = NodeRemovalService(self.db, SimpleNamespace(
            decommission=lambda *args: phases.append(args)), unavailable_inventory)
        worker.request(self.actor, 'n1')
        self.assertFalse(worker.run_one())
        self.assertEqual(worker.get(self.actor, 'n1')['removal_status'], 'blocked')
        self.assertTrue(self.db.connection.execute('SELECT enabled FROM backend_nodes WHERE key=?', ('n1',)).fetchone()[0])
        self.assertIsNone(self.db.connection.execute('SELECT * FROM backend_node_drains WHERE node_key=?', ('n1',)).fetchone())
        self.assertEqual(phases, [])
