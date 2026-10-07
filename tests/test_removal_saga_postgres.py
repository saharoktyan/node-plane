"""Real PostgreSQL retirement with explicit driver/host-observation fixtures."""
import os
from types import SimpleNamespace
import unittest
from uuid import uuid4

from backend.authorization import AccessDenied
from backend.executor import IntentExecutor
from backend.node_lifecycle import NodeLifecycle
from backend.node_removal import NodeRemovalService
from backend.profile_commands import ProfileCommands
from backend.profiles import ProfileRepository
from backend.removal_inventory import inventory_digest
from backend.removal_verifier import RemovalVerificationError
from backend.system_cleanup import SystemCleanupService
from backend.identity_repository import SQLIdentityRepository
from backend.agent_rollout import AgentRolloutService
from backend.backups import BackupService
from backend.config_issuance import ConfigIssuanceService
from tests import test_backups_postgres as fixture
from tests.test_core_reliability_postgres import JournalDriver

RESOURCES = {'paths': ['/opt/node-plane-runtime'], 'containers': ['xray', 'amnezia-awg']}


@unittest.skipUnless(os.environ.get('NODE_PLANE_TEST_POSTGRES_DSN'), 'requires disposable PostgreSQL')
class RemovalSagaPostgresTests(unittest.TestCase):
    setUp = fixture.BackupsPostgresTests.setUp
    drop_schema = fixture.BackupsPostgresTests.drop_schema

    def node(self, key='node', applied=1):
        with self.db.transaction() as conn:
            conn.execute("""INSERT INTO backend_nodes(key,title,region,protocols_json,applied_revision)
                VALUES (?,?,'Europe','["awg","xray"]',?)""", (key, key, applied))
            conn.execute('INSERT INTO backend_node_connections(node_key,transport,ssh_target) VALUES (?,\'local\',NULL)', (key,))

    def grant(self, node='node'):
        profile = ProfileRepository(self.db).create_profile(runtime_name='p_' + uuid4().hex,
            display_name='Test profile')
        ProfileCommands(self.db).execute(self.actor, str(uuid4()), action='grants', profile_id=profile,
            revision=1, values={'grants': [{'node_key': node, 'protocol': protocol} for protocol in ('awg', 'xray')]})
        return profile

    def verifier(self, target, final=False):
        return SimpleNamespace(local=True, ssh_target=None,
            capture_identity=lambda key: 'a' * 64,
            capture_resources=lambda key, expected: RESOURCES,
            verify=lambda key, expected, resources: {'result': 'agent_and_standard_artifacts_absent',
                'inventory_digest': inventory_digest(resources), 'method': 'local', 'target': 'local',
                'host_fingerprint': expected, 'checked_at': '2026-10-06T00:00:00+00:00'})

    def test_unprovisioned_entry_retires_without_driver_or_host_contact(self):
        self.node(applied=0)
        def forbidden(*args):
            raise AssertionError('unused registry entry must not contact a host')
        worker = NodeRemovalService(self.db, verifier_factory=forbidden)
        worker.request(self.actor, 'node')
        self.assertTrue(worker.run_one())
        self.assertEqual(worker.get(self.actor, 'node')['status'], 'removed_unprovisioned')

    def test_pending_grants_are_revoked_before_cleanup_and_other_nodes_survive(self):
        self.node()
        self.node('other')
        removed_profile, untouched_profile = self.grant(), self.grant('other')
        driver, phases = JournalDriver(), []
        driver.decommission = lambda key, identity, phase: phases.append(phase)
        worker = NodeRemovalService(self.db, driver, self.verifier)
        worker.request(self.actor, 'node')
        self.assertTrue(worker.run_one())
        self.assertFalse(worker.run_one())
        self.assertEqual(phases, [])
        executor = IntentExecutor(self.db, driver)
        while executor.run_one():
            pass
        self.assertEqual([intent['action'] for _, intent in driver.calls if intent['node_key'] == 'node'], ['delete', 'delete'])
        for _ in range(4):
            self.assertTrue(worker.run_one())
        self.assertEqual(worker.get(self.actor, 'node')['status'], 'removed')
        self.assertEqual(phases, ['prepare', 'delete_runtime', 'uninstall'])
        with self.db.connect() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM backend_nodes WHERE key='node'").fetchone())
            self.assertIsNone(conn.execute('SELECT 1 FROM backend_grants WHERE profile_id=?', (removed_profile,)).fetchone())
            self.assertEqual(conn.execute('SELECT COUNT(*) AS count FROM backend_grants WHERE profile_id=?', (untouched_profile,)).fetchone()['count'], 2)
            self.assertEqual(conn.execute("SELECT mode FROM backend_node_retirements WHERE node_key='node'").fetchone()['mode'], 'verified')
        self.assertFalse(executor.run_one())
        self.assertFalse(worker.run_one())

    def test_registry_only_retirement_supersedes_uncertain_tasks_without_claiming_host_cleanup(self):
        self.node()
        profile = self.grant()
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_operation_tasks SET status='blocked'")
        lifecycle = NodeLifecycle(self.db)
        lifecycle.start_drain(self.actor, 'node')
        result = lifecycle.retire_registry_only(self.actor, 'node', 'VPS hosting expired; remote state unknown.')
        with self.db.connect() as conn:
            retirement = conn.execute("SELECT * FROM backend_node_retirements WHERE node_key='node'").fetchone()
            self.assertEqual(retirement['mode'], 'registry_only')
            self.assertEqual(retirement['unfinished_tasks'], 4)
            self.assertEqual(result['unfinished_tasks'], 4)
            self.assertIsNone(retirement['evidence_json'])
            self.assertIsNone(conn.execute("SELECT 1 FROM backend_grants WHERE node_key='node'").fetchone())
            self.assertFalse(conn.execute("SELECT 1 FROM backend_operation_tasks WHERE node_key='node' AND status IN ('awaiting_executor','running','blocked')").fetchone())
        driver = JournalDriver()
        self.assertFalse(IntentExecutor(self.db, driver).run_one())
        self.assertEqual(driver.calls, [])
        with self.assertRaises(AccessDenied):
            ProfileCommands(self.db).execute(self.actor, str(uuid4()), action='grants', profile_id=profile,
                revision=ProfileRepository(self.db).get(profile)['desired_revision'],
                values={'grants': [{'node_key': 'node', 'protocol': 'awg'}]})

    def test_registry_retirement_abandons_rollout_without_replay_or_permanent_restore_block(self):
        runner = lambda args: self.fail('retired node rollout must not run')
        rollout = AgentRolloutService(self.db, runner)
        for status in ('awaiting_executor', 'running', 'blocked'):
            key = 'lost_' + status
            self.node(key)
            task = rollout.request(self.actor, key, str(uuid4()), transport='local')
            with self.db.transaction() as conn:
                conn.execute('UPDATE backend_agent_rollouts SET status=? WHERE id=?', (status, task['id']))
            with self.db.connect() as conn:
                self.assertTrue(BackupService.busy(conn))
            reason = 'Operator accepts unknown remote state after losing VPS access.'
            result = NodeLifecycle(self.db).retire_registry_only(self.actor, key, reason)
            self.assertEqual(result['mode'], 'registry_only')
            self.assertEqual(rollout.get(self.actor, task['id'])['status'], 'blocked')
            with self.db.connect() as conn:
                self.assertFalse(BackupService.busy(conn))
                tombstone = conn.execute('SELECT * FROM backend_node_retirements WHERE node_key=?', (key,)).fetchone()
                self.assertIsNone(tombstone['evidence_json'])
                self.assertEqual(tombstone['reason'], reason)
            self.assertFalse(rollout.run_one())
            with self.assertRaises(AccessDenied):
                rollout.request(self.actor, key, str(uuid4()), transport='local')

    def test_registry_retirement_rechecks_cached_administrator_before_draining(self):
        self.node()
        self.grant()
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_accounts SET role='member' WHERE id=?", (self.admin.id,))
        for action in (
            lambda: NodeLifecycle(self.db).retire_registry_only(self.actor, 'node', 'VPS access permanently lost; remote state unverified.'),
            lambda: NodeLifecycle(self.db).start_drain(self.actor, 'node')):
            with self.assertRaisesRegex(AccessDenied, 'permission_denied'):
                action()
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT enabled FROM backend_nodes WHERE key='node'").fetchone()['enabled'], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) AS count FROM backend_grants WHERE node_key='node'").fetchone()['count'], 2)
            self.assertIsNone(conn.execute("SELECT 1 FROM backend_node_retirements WHERE node_key='node'").fetchone())

    def test_retirement_invalidates_open_config_screen_and_fences_pending_issuance(self):
        self.node()
        profile = self.grant()
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_nodes SET xray_transports_json='[\"tcp\",\"xhttp\"]' WHERE key='node'")
        executor = IntentExecutor(self.db, JournalDriver())
        while executor.run_one():
            pass
        service = ConfigIssuanceService(self.db)
        issuance = service.request(self.actor, profile, 'node', 'xray', 'tcp', str(uuid4()))
        self.assertEqual(service.get(self.actor, issuance['id'])['status'], 'awaiting_executor')
        NodeLifecycle(self.db).retire_registry_only(self.actor, 'node', 'VPS access lost; remote execution cannot be verified.')
        with self.assertRaisesRegex(AccessDenied, 'grant_revoked'):
            service.get(self.actor, issuance['id'])
        with self.assertRaisesRegex(AccessDenied, 'grant_revoked'):
            service.request(self.actor, profile, 'node', 'xray', 'tcp', str(uuid4()))
        with self.db.connect() as conn:
            row = conn.execute('SELECT status FROM backend_config_issuances WHERE id=?', (issuance['id'],)).fetchone()
            self.assertEqual(row['status'], 'superseded')
        self.assertFalse(service.run_one())

    def test_snapshot_after_registry_retirement_restores_without_phantom_grants(self):
        self.node()
        profile = self.grant()
        rollout = AgentRolloutService(self.db).request(self.actor, 'node', str(uuid4()), transport='local')
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_agent_rollouts SET status='blocked' WHERE id=?", (rollout['id'],))
        NodeLifecycle(self.db).retire_registry_only(self.actor, 'node', 'VPS expired; operator explicitly abandons remote cleanup.')
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT mode FROM backend_node_retirements WHERE node_key='node'").fetchone()['mode'], 'registry_only')
        backup_id = self.service.create_snapshot()['backup_id']
        checksum = self.service.detail(self.actor, backup_id)['checksum']
        job = self.service.queue(self.actor, str(uuid4()), 'restore', backup_id, checksum)
        self.assertTrue(self.service.run_one())
        self.assertTrue(self.service.run_one())
        self.assertEqual(self.service.get(self.actor, job['id'])['status'], 'succeeded')
        with self.db.connect() as conn:
            self.assertIsNotNone(conn.execute('SELECT 1 FROM backend_profiles WHERE id=?', (profile,)).fetchone())
            self.assertIsNone(conn.execute("SELECT 1 FROM backend_nodes WHERE key='node'").fetchone())
            self.assertIsNone(conn.execute("SELECT 1 FROM backend_grants WHERE node_key='node'").fetchone())
            # Configuration restore deliberately resets operational history;
            # retirement evidence is not part of the configuration snapshot.
            self.assertIsNone(conn.execute("SELECT 1 FROM backend_node_retirements WHERE node_key='node'").fetchone())
            self.assertIsNone(conn.execute("SELECT 1 FROM backend_agent_rollouts WHERE node_key='node'").fetchone())

    def test_failed_final_host_verification_preserves_blocked_retirement(self):
        self.node()
        def verifier(target, final=False):
            value = self.verifier(target, final)
            if final:
                def fail(*args):
                    raise RemovalVerificationError('managed_container_present')
                value.verify = fail
            return value
        phases = []
        worker = NodeRemovalService(self.db, SimpleNamespace(
            decommission=lambda key, identity, phase: phases.append(phase)), verifier)
        worker.request(self.actor, 'node')
        for _ in range(4):
            self.assertTrue(worker.run_one())
        self.assertFalse(worker.run_one())
        state = worker.get(self.actor, 'node')
        self.assertEqual(state['removal_status'], 'blocked')
        self.assertEqual(state['error_code'], 'host_verification_failed')
        with self.db.connect() as conn:
            self.assertIsNotNone(conn.execute("SELECT 1 FROM backend_nodes WHERE key='node' AND enabled=0").fetchone())
            self.assertIsNone(conn.execute("SELECT 1 FROM backend_node_retirements WHERE node_key='node'").fetchone())

    def controller(self):
        events = []
        host = SimpleNamespace(
            deployment=lambda: {'base_dir': '/opt/node-plane', 'shared_dir': '/opt/node-plane/shared',
                                'postgres_container': None, 'units': []},
            clear_local=lambda *args: events.append('clear'),
            prepare_uninstall=lambda *args: {'unit': 'fixture', 'script': '/not-executed'},
            launch_uninstall=lambda *args: events.append('launch'))
        with self.db.transaction() as conn:
            conn.execute('CREATE TABLE unrelated_application(value TEXT)')
            conn.execute("INSERT INTO unrelated_application VALUES ('preserve')")
        ProfileRepository(self.db).create_profile(runtime_name='controller_test', display_name='Controller test')
        self.node()
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_node_removal_inventories VALUES ('node','{}')")
        service = SystemCleanupService(self.db, host=host, backups=self.service)
        return service, events

    def test_controller_reset_wipes_owned_tables_but_preserves_admin_and_unrelated_table(self):
        service, events = self.controller()
        plan = service.plan(self.actor, 'reset', False)
        job = service.queue(self.actor, plan['id'], plan['confirmation_phrase'], str(uuid4()))
        for _ in range(4):
            self.assertTrue(service.run_one())
        self.assertEqual(service.get(self.actor, job['id'])['status'], 'succeeded')
        self.assertEqual(SQLIdentityRepository(self.db).find_telegram_account(101).id, self.admin.id)
        self.assertEqual(events, ['clear'])
        with self.db.connect() as conn:
            for table in ('backend_profiles', 'backend_nodes', 'backend_node_removal_inventories'):
                self.assertEqual(conn.execute(f'SELECT COUNT(*) AS count FROM {table}').fetchone()['count'], 0)
            self.assertEqual(conn.execute('SELECT value FROM unrelated_application').fetchone()['value'], 'preserve')
        self.assertFalse(service.run_one())

    def test_controller_remove_wipes_owned_tables_and_launches_only_after_acknowledgment(self):
        service, events = self.controller()
        plan = service.plan(self.actor, 'remove', False)
        job = service.queue(self.actor, plan['id'], plan['confirmation_phrase'], str(uuid4()))
        for _ in range(3):
            self.assertTrue(service.run_one())
        self.assertEqual(service.get(self.actor, job['id'])['status'], 'awaiting_shutdown')
        self.assertFalse(service.run_one())
        self.assertEqual(events, [])
        service.acknowledge_shutdown(self.actor, job['id'])
        self.assertTrue(service.run_one())
        self.assertFalse(service.run_one())
        self.assertEqual(events, ['launch'])
        with self.db.connect() as conn:
            for table in ('backend_accounts', 'backend_profiles', 'backend_nodes', 'backend_system_cleanup_jobs'):
                self.assertEqual(conn.execute(f'SELECT COUNT(*) AS count FROM {table}').fetchone()['count'], 0)
            self.assertEqual(conn.execute('SELECT value FROM unrelated_application').fetchone()['value'], 'preserve')
