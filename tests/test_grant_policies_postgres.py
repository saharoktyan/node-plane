from concurrent.futures import ThreadPoolExecutor
import os
import threading
from types import SimpleNamespace
import unittest
from uuid import uuid4

from backend.authorization import AccessDenied
from backend.grant_policies import GrantPolicies
from backend.node_bootstrap import NodeBootstrapService
from backend.node_operations import NodeOperations
from backend.nodes import NodeService
from backend.profile_commands import ProfileCommands
from tests import test_backups_postgres as fixture


@unittest.skipUnless(os.environ.get('NODE_PLANE_TEST_POSTGRES_DSN'), 'requires disposable PostgreSQL')
class GrantPoliciesPostgresTests(unittest.TestCase):
    setUp = fixture.BackupsPostgresTests.setUp
    drop_schema = fixture.BackupsPostgresTests.drop_schema

    def profile(self):
        profile = ProfileCommands(self.db).execute(self.actor, str(uuid4()), action='create',
            values={'display_name': 'Alice'})['profile']['id']
        from backend.devices import DeviceRepository
        from backend.profiles import ProfileRepository
        with self.db.transaction() as conn:
            DeviceRepository.ensure_default(conn, ProfileRepository(self.db).get(profile))
        return profile

    def values(self, protocols=None):
        return {'explicit_grants': [], 'rules': [{'scope': 'all', 'region_id': None,
                'protocols': protocols or ['awg']}], 'exclusions': []}

    def policy(self, profile, protocols=None):
        return ProfileCommands(self.db).execute(self.actor, str(uuid4()), action='policy',
            profile_id=profile, revision=1, values=self.values(protocols))

    def test_competing_edits_preserve_revision_and_exact_command_identity(self):
        profile = self.profile()
        start = threading.Barrier(2)

        def edit(protocol):
            start.wait(timeout=5)
            try:
                return self.policy(profile, [protocol])
            except AccessDenied as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(edit, 'awg'), pool.submit(edit, 'xray')
            results = [first.result(timeout=15), second.result(timeout=15)]
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertIn('revision_conflict', results)
        self.assertEqual(GrantPolicies(self.db).get(self.actor, profile)['revision'], 2)

    def test_policy_racing_with_confirmed_bootstrap_cannot_miss_new_server(self):
        profile = self.profile()
        NodeService(self.db).command(self.actor, action='create', command_key=str(uuid4()), values={
            'key': 'n1', 'title': 'Node', 'region': 'Europe', 'transport': 'ssh', 'ssh_target': 'root@n1.example',
            'protocols': ['awg'], 'settings': {'public_host': 'n1.example'}})
        with self.assertRaises(AccessDenied) as error:
            NodeOperations(self.db).queue(self.actor, 'n1', 'bootstrap', revision=1, command_key=str(uuid4()))
        self.assertEqual(error.exception.code, 'node_installation_required')
        self.assertEqual(error.exception.status, 409)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) AS count FROM backend_node_jobs WHERE node_key=?', ('n1',)).fetchone()['count'], 0)
        driver = SimpleNamespace(
            inspect_node_services=lambda node: {'docker': True, 'awg_config_valid': True,
                'awg_running': True, 'xray_config_valid': True, 'xray_running': True},
            node_action=lambda task, action, intent: {
                'node_key': 'n1', 'action': action, 'revision': intent['revision'], 'result': {'verified': True}})
        parent = NodeBootstrapService(self.db, driver)
        bootstrap_job = parent.queue(self.actor, 'n1', 1, str(uuid4()))
        for _ in range(3):
            self.assertTrue(parent.run_one())
        start = threading.Barrier(2)

        def policy():
            start.wait(timeout=5)
            return self.policy(profile)

        def bootstrap():
            start.wait(timeout=5)
            return NodeOperations(self.db, driver).run_one()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(policy), pool.submit(bootstrap)
            first.result(timeout=15)
            self.assertTrue(second.result(timeout=15))
        self.assertTrue(parent.run_one())
        self.assertEqual(parent.get(self.actor, bootstrap_job['id'])['status'], 'succeeded')
        with self.db.connect() as conn:
            grants = conn.execute('SELECT node_key,protocol FROM backend_grants WHERE profile_id=?', (profile,)).fetchall()
            self.assertEqual([dict(g) for g in grants], [{'node_key': 'n1', 'protocol': 'awg'}])
            tasks = conn.execute('SELECT t.action FROM backend_operation_tasks t JOIN backend_operations o ON o.id=t.operation_id WHERE o.profile_id=?', (profile,)).fetchall()
            self.assertEqual([t['action'] for t in tasks], ['ensure'])

    def test_backup_restores_policies_regions_and_separate_manual_sources(self):
        profile = self.profile()
        NodeService(self.db).command(self.actor, action='create', command_key=str(uuid4()), values={
            'key': 'n1', 'title': 'Node', 'region': 'Europe', 'transport': 'ssh', 'ssh_target': 'root@n1.example',
            'protocols': ['awg'], 'settings': {'public_host': 'n1.example'}})
        original = self.policy(profile)
        region_id = NodeService(self.db).get(self.actor, 'n1')['region_id']
        backup = self.service.create_snapshot()['backup_id']
        ProfileCommands(self.db).execute(self.actor, str(uuid4()), action='policy', profile_id=profile,
            revision=original['profile']['desired_revision'], values={'explicit_grants': [], 'rules': [], 'exclusions': []})
        job = self.service.queue(self.actor, str(uuid4()), 'restore', backup,
                                 self.service.detail(self.actor, backup)['checksum'])
        while self.service.run_one():
            pass
        self.assertEqual(self.service.get(self.actor, job['id'])['status'], 'succeeded')
        values = GrantPolicies(self.db).get(self.actor, profile)
        self.assertEqual(values['rules'], self.values()['rules'])
        self.assertEqual(values['explicit_grants'], [])
        self.assertEqual(NodeService(self.db).get(self.actor, 'n1')['region_id'], region_id)
