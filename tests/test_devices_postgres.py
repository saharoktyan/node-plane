"""Device identity migration checks against disposable PostgreSQL only."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import threading
import unittest
from uuid import uuid4

from backend.backups import encoded
from backend.devices import DeviceRepository
from backend.device_commands import DeviceCommands
from backend.authorization import AccessDenied
from backend.config_issuance import ConfigIssuanceService
from backend.executor import IntentExecutor
from backend.operations import OperationRepository
from backend.profiles import ProfileRepository
from backend.system_settings import SystemSettingsService
from backend.traffic import TrafficService
from tests import test_backups_postgres as fixture


@unittest.skipUnless(os.environ.get('NODE_PLANE_TEST_POSTGRES_DSN'), 'requires disposable PostgreSQL')
class DevicesPostgresTests(unittest.TestCase):
    setUp = fixture.BackupsPostgresTests.setUp
    drop_schema = fixture.BackupsPostgresTests.drop_schema

    def test_concurrent_custom_devices_share_parent_revision_fence(self):
        profile_id = ProfileRepository(self.db).create_profile(runtime_name='alice', display_name='Alice')
        start = threading.Barrier(2)

        def create(name):
            start.wait(timeout=5)
            try:
                return DeviceCommands(self.db).execute(self.actor, str(uuid4()),
                    action='create', profile_id=profile_id, revision=1, display_name=name)
            except AccessDenied as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(create, 'Phone'), pool.submit(create, 'phone')
            results = [first.result(timeout=15), second.result(timeout=15)]
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertIn('revision_conflict', results)
        self.assertEqual(len(DeviceRepository(self.db).list_for_profile(profile_id)), 1)

    def test_custom_device_delete_retires_after_executor_confirmation(self):
        from tests.test_backend_device_peers import PeerDriver
        profile_id = ProfileRepository(self.db).create_profile(runtime_name='alice', display_name='Alice')
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_nodes(key,title,region,protocols_json) VALUES ('node','Node','Europe','[\"awg\"]')")
            conn.execute("INSERT INTO backend_grants VALUES (?,'node','awg')", (profile_id,))
        commands = DeviceCommands(self.db)
        created = commands.execute(self.actor, str(uuid4()), action='create',
            profile_id=profile_id, revision=1, display_name='Phone')
        driver = PeerDriver()
        executor = IntentExecutor(self.db, driver)
        while executor.run_one():
            pass
        self.assertEqual(len(driver.peers), 1)
        deleted = commands.execute(self.actor, str(uuid4()), action='delete',
            profile_id=profile_id, device_id=created['device']['id'], revision=1)
        self.assertEqual(deleted['device']['status'], 'deleting')
        while executor.run_one():
            pass
        self.assertFalse(driver.peers)
        self.assertEqual(DeviceRepository(self.db).list_for_profile(profile_id)[0]['status'], 'retired')

    def test_pre_device_task_and_issuance_migration_preserves_command_identity(self):
        profile_id = ProfileRepository(self.db).create_profile(runtime_name='alice',display_name='Alice')
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_nodes(key,title,region,protocols_json) VALUES ('node','Node','Europe','[\"awg\"]')")
            conn.execute("INSERT INTO backend_grants VALUES (?,'node','awg')",(profile_id,))
            OperationRepository.record(conn,self.actor,profile_id,set())
            task = dict(conn.execute('SELECT * FROM backend_operation_tasks LIMIT 1').fetchone())
            conn.execute('DROP INDEX backend_operation_task_target')
            conn.execute('ALTER TABLE backend_operation_tasks DROP COLUMN device_id')
            conn.execute('''ALTER TABLE backend_operation_tasks ADD CONSTRAINT
                backend_operation_tasks_operation_id_node_key_protocol_key UNIQUE(operation_id,node_key,protocol)''')
            conn.execute('ALTER TABLE backend_config_issuances DROP COLUMN device_id')
            conn.execute('ALTER TABLE backend_config_issuances DROP COLUMN device_revision')
            issuance_id = str(uuid4())
            conn.execute('''INSERT INTO backend_config_issuances
                (id,actor_account_id,command_key,profile_id,node_key,protocol,transport,profile_revision,node_revision,status,created_at,expires_at)
                VALUES (?,?,?,?,'node','awg','vpn',1,1,'awaiting_executor','2026-10-07','2026-10-08')''',
                (issuance_id,self.admin.id,str(uuid4()),profile_id))
        OperationRepository(self.db).initialize_schema()
        ConfigIssuanceService(self.db).initialize_schema()
        device_id = DeviceRepository(self.db).list_for_profile(profile_id)[0]['id']
        with self.db.transaction() as conn:
            migrated = conn.execute('SELECT * FROM backend_operation_tasks WHERE id=?',(task['id'],)).fetchone()
            self.assertEqual(migrated['intent_json'],task['intent_json'])
            self.assertEqual(migrated['device_id'],device_id)
            issuance = conn.execute('SELECT device_id,device_revision FROM backend_config_issuances WHERE id=?',(issuance_id,)).fetchone()
            self.assertEqual((issuance['device_id'],issuance['device_revision']),(device_id,1))
            # The previous three-column constraint must no longer reject an
            # independent peer within the same operation/node/protocol.
            conn.execute('''INSERT INTO backend_operation_tasks
                (id,operation_id,node_key,protocol,device_id,action,status,intent_json)
                VALUES (?,?,'node','awg',?,'ensure','awaiting_executor',?)''',
                (str(uuid4()),task['operation_id'],str(uuid4()),task['intent_json']))

    def test_multi_peer_outbox_and_counters_use_distinct_runtime_names(self):
        from tests.test_backend_device_peers import PeerDriver
        profile_id = ProfileRepository(self.db).create_profile(runtime_name='alice',display_name='Alice')
        with self.db.transaction() as conn:
            conn.execute('''INSERT INTO backend_nodes
                (key,title,region,protocols_json,applied_revision,settings_json)
                VALUES ('node','Node','Europe','["awg"]',1,'{"public_host":"node.example","awg_port":51820}')''')
            conn.execute("INSERT INTO backend_grants VALUES (?,'node','awg')",(profile_id,))
            profile = conn.execute('SELECT * FROM backend_profiles WHERE id=?',(profile_id,)).fetchone()
            DeviceRepository.ensure_default(conn,profile)
            phone_id = str(uuid4())
            conn.execute('''INSERT INTO backend_devices (id,profile_id,display_name,runtime_name,status,created_at)
                VALUES (?,?,'Phone','phone_peer','active','2026-10-07')''',(phone_id,profile_id))
            OperationRepository.record(conn,self.actor,profile_id,set())
        driver = PeerDriver()
        executor = IntentExecutor(self.db,driver)
        while executor.run_one():
            pass
        self.assertEqual(set(driver.peers),{'alice','phone_peer'})
        SystemSettingsService(self.db).update_traffic_policy(self.actor,True)
        traffic = TrafficService(self.db,driver)
        self.assertTrue(traffic.scheduled())
        driver.counters['alice'][0] += 100
        driver.counters['phone_peer'][0] += 200
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM backend_system_settings WHERE key='traffic_last_scan'")
        self.assertTrue(traffic.scheduled())
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT uplink_bytes FROM backend_traffic_usage WHERE profile_id=?",(profile_id,)).fetchone()['uplink_bytes'],300)
            self.assertEqual(conn.execute('SELECT COUNT(*) AS count FROM backend_traffic_peers').fetchone()['count'],2)

    def test_concurrent_adoption_preserves_parent_revision(self):
        profile_id = ProfileRepository(self.db).create_profile(runtime_name='alice', display_name='Alice')
        start = threading.Barrier(2)

        def adopt():
            with self.db.transaction() as conn:
                profile = conn.execute('SELECT * FROM backend_profiles WHERE id=?', (profile_id,)).fetchone()
                start.wait(timeout=5)
                return DeviceRepository.ensure_default(conn, profile)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(adopt), pool.submit(adopt)
            self.assertEqual(first.result(timeout=15), second.result(timeout=15))
        self.assertEqual(len(DeviceRepository(self.db).list_for_profile(profile_id)), 1)
        self.assertEqual(ProfileRepository(self.db).get(profile_id)['desired_revision'], 1)

    def test_restore_preserves_device_identity_and_label(self):
        profile_id = ProfileRepository(self.db).create_profile(runtime_name='alice', display_name='Alice')
        with self.db.transaction() as conn:
            profile = conn.execute('SELECT * FROM backend_profiles WHERE id=?', (profile_id,)).fetchone()
            device_id = DeviceRepository.ensure_default(conn, profile)
            conn.execute("UPDATE backend_devices SET display_name='Phone' WHERE id=?", (device_id,))
        backup_id = self.service.create_snapshot()['backup_id']
        checksum = self.service.detail(self.actor, backup_id)['checksum']
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_devices SET display_name='Changed',status='retired',revision=2 WHERE id=?", (device_id,))
        job = self.service.queue(self.actor, str(uuid4()), 'restore', backup_id, checksum)
        self.assertTrue(self.service.run_one())
        self.assertTrue(self.service.run_one())
        self.assertEqual(self.service.get(self.actor, job['id'])['status'], 'succeeded')
        device = DeviceRepository(self.db).list_for_profile(profile_id)[0]
        self.assertEqual((device['id'], device['display_name'], device['status']), (device_id, 'Phone', 'active'))
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT runtime_name FROM backend_devices WHERE id=?', (device_id,)).fetchone()['runtime_name'], 'alice')

    def test_existing_grant_migration_and_v1_backup_are_compatible(self):
        profile_id = ProfileRepository(self.db).create_profile(runtime_name='legacy_awg', display_name='Legacy')
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_nodes(key,title,region,protocols_json) VALUES ('node','Node','Europe','[\"awg\"]')")
            conn.execute("INSERT INTO backend_grants VALUES (?,'node','awg')", (profile_id,))
        OperationRepository(self.db).initialize_schema()
        device = DeviceRepository(self.db).list_for_profile(profile_id)[0]
        backup_id = self.service.create_snapshot()['backup_id']
        path = self.service._path(backup_id)
        payload = json.loads(path.read_bytes())
        del payload['tables']['backend_devices']
        from backend.grant_policies import TABLE_COLUMNS
        for table in TABLE_COLUMNS:
            del payload['tables'][table]
        payload['format'] = 'node-plane-backend-v1'
        payload['checksum'] = hashlib.sha256(encoded(payload['tables'])).hexdigest()
        path.write_bytes(encoded(payload))
        self.assertTrue(self.service.detail(self.actor, backup_id)['compatible'])
        converted = self.service._load(backup_id)['tables']['backend_devices']['rows'][0]
        self.assertEqual((converted['id'], converted['runtime_name']), (device['id'], 'legacy_awg'))
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) AS count FROM backend_operation_tasks').fetchone()['count'], 0)
