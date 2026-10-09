"""Run separately against NODE_PLANE_TEST_POSTGRES_DSN, never production data."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import os
import tempfile
import threading
import unittest
from uuid import uuid4

from db.postgres_db import PostgresDB
from db.migrations import REVISIONS, Revision, MigrationError, migrate, check_schema, schema_status


@unittest.skipUnless(os.environ.get('NODE_PLANE_TEST_POSTGRES_DSN'), 'requires disposable PostgreSQL')
class MigrationsPostgresTests(unittest.TestCase):
    def setUp(self):
        from psycopg.conninfo import make_conninfo
        self.schema = 'migration_test_' + uuid4().hex
        self.base = PostgresDB(os.environ['NODE_PLANE_TEST_POSTGRES_DSN'])
        with self.base.transaction() as conn:
            conn.execute(f'CREATE SCHEMA {self.schema}')
        self.addCleanup(self.drop_schema)
        self.db = PostgresDB(make_conninfo(self.base.dsn, options=f'-c search_path={self.schema}'))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def drop_schema(self):
        with self.base.transaction() as conn:
            conn.execute(f'DROP SCHEMA {self.schema} CASCADE')

    def test_clean_database_and_repeat_are_noops(self):
        self.assertFalse(schema_status(self.db)['ready'])
        self.assertEqual(migrate(self.db)['applied'], [r.number for r in REVISIONS])
        self.assertEqual(migrate(self.db)['applied'], [])
        self.assertTrue(check_schema(self.db)['ready'])
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) AS n FROM backend_schema_revisions').fetchone()['n'],len(REVISIONS))
            self.assertEqual(conn.execute('SELECT revision FROM backend_account_guard WHERE id=1').fetchone()['revision'],1)
            conn.execute('SELECT device_id FROM backend_operation_tasks LIMIT 1')
            conn.execute('SELECT node_key FROM backend_node_traffic LIMIT 1')

    def test_upgrade_populated_baseline_preserves_data_and_seeds_traffic_once(self):
        migrate(self.db,revisions=REVISIONS[:1])
        from backend.admin_cli import bootstrap_admin
        from backend.identity_repository import SQLIdentityRepository
        from backend.credentials import CredentialService, ADAPTER_SCOPES
        from backend.authorization import PrincipalKind, Actor, Principal, ADMIN_PERMISSIONS
        from backend.profiles import ProfileRepository
        from backend.operations import OperationRepository
        identities = SQLIdentityRepository(self.db)
        admin = bootstrap_admin(identities,101,self.db)
        token_id, token = CredentialService(self.db).issue(PrincipalKind.ADAPTER,ADAPTER_SCOPES)
        profile = ProfileRepository(self.db).create_profile(runtime_name='alice',display_name='Alice',owner_account_id=admin.id)
        now = datetime.now(timezone.utc)
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_nodes(key,title,region,protocols_json) VALUES ('lv1','Latvia #1','Europe','[\"awg\"]')")
            conn.execute("INSERT INTO backend_grants VALUES (?,'lv1','awg')",(profile,))
            actor = Actor(Principal('migration',PrincipalKind.SERVICE,ADMIN_PERMISSIONS),admin)
            OperationRepository.record(conn,actor,profile,set())
            conn.execute('''INSERT INTO backend_traffic_usage
                (profile_id,account_id,node_key,protocol,uplink_bytes,downlink_bytes,tracked_since,last_sample_at,status,period_month)
                VALUES (?,?,'lv1','awg',100,200,?,?,'current',?)''',
                (profile,admin.id,now.isoformat(),now.isoformat(),now.strftime('%Y-%m')))
            conn.execute("INSERT INTO schema_meta VALUES ('updates_auto_check_enabled','1')")
            before = [dict(r) for r in conn.execute('SELECT * FROM backend_operation_tasks ORDER BY id').fetchall()]
            devices = [dict(r) for r in conn.execute('SELECT * FROM backend_devices ORDER BY id').fetchall()]
        self.assertEqual(migrate(self.db)['applied'],[r.number for r in REVISIONS[1:]])
        self.assertEqual(migrate(self.db)['applied'],[])
        with self.db.connect() as conn:
            self.assertEqual([dict(r) for r in conn.execute('SELECT * FROM backend_operation_tasks ORDER BY id').fetchall()],before)
            self.assertEqual([dict(r) for r in conn.execute('SELECT * FROM backend_devices ORDER BY id').fetchall()],devices)
            self.assertEqual(conn.execute('SELECT uplink_bytes+downlink_bytes AS total FROM backend_node_traffic').fetchone()['total'],300)
            self.assertEqual(conn.execute("SELECT value FROM schema_meta WHERE key='updates_auto_check_enabled'").fetchone()['value'],'1')
        self.assertEqual(CredentialService(self.db).authenticate('Bearer '+token).id,token_id)
        self.assertEqual(ProfileRepository(self.db).get(profile)['display_name'],'Alice')

    def test_existing_unversioned_install_is_adopted_without_reinitialization(self):
        # This fixture is the previous deployment command, not the migration runner.
        from backend.identity_repository import SQLIdentityRepository
        from backend.credentials import CredentialService
        from backend.profiles import ProfileRepository
        from backend.profile_commands import ProfileCommands
        from backend.access_requests import AccessRequestService
        from backend.accounts import AccountService
        from backend.nodes import NodeService
        from backend.node_settings import NodeSettingsService
        from backend.config_issuance import ConfigIssuanceService
        from backend.agent_rollout import AgentRolloutService
        for cls in (SQLIdentityRepository,CredentialService,ProfileRepository,ProfileCommands,
                    AccessRequestService,AccountService,NodeService,NodeSettingsService,
                    ConfigIssuanceService,AgentRolloutService):
            cls(self.db).initialize_schema()
        account = SQLIdentityRepository(self.db).create_account()
        profile = ProfileRepository(self.db).create_profile(runtime_name='existing',display_name='Existing',owner_account_id=account.id)
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_nodes(key,title,region,protocols_json) VALUES ('node','Node','Europe','[]')")
            conn.execute("INSERT INTO backend_node_traffic VALUES ('node','xray',?,123,456,?,?)", ('2026-10','2026-10-09','2026-10-09'))
        self.assertEqual(migrate(self.db)['applied'],[r.number for r in REVISIONS])
        self.assertEqual(ProfileRepository(self.db).get(profile)['runtime_name'],'existing')
        with self.db.connect() as conn:
            row = conn.execute('SELECT * FROM backend_node_traffic').fetchone()
            self.assertEqual(row['uplink_bytes'],123)
            self.assertEqual(row['downlink_bytes'],456)

    def test_pre_device_columns_are_upgraded_without_changing_command_payloads(self):
        migrate(self.db,revisions=REVISIONS[:1])
        from backend.admin_cli import bootstrap_admin
        from backend.identity_repository import SQLIdentityRepository
        from backend.profiles import ProfileRepository
        from backend.operations import OperationRepository
        from backend.authorization import Actor, Principal, PrincipalKind, ADMIN_PERMISSIONS
        admin = bootstrap_admin(SQLIdentityRepository(self.db),101,self.db)
        profile = ProfileRepository(self.db).create_profile(runtime_name='peer',display_name='Peer')
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_nodes(key,title,region,protocols_json) VALUES ('node','Node','Europe','[\"awg\"]')")
            conn.execute("INSERT INTO backend_grants VALUES (?,'node','awg')",(profile,))
            OperationRepository.record(conn,Actor(Principal('local',PrincipalKind.SERVICE,ADMIN_PERMISSIONS),admin),profile,set())
            task = dict(conn.execute('SELECT * FROM backend_operation_tasks LIMIT 1').fetchone())
            conn.execute('DELETE FROM backend_schema_revisions')
            conn.execute('DROP INDEX backend_operation_task_target')
            conn.execute('ALTER TABLE backend_operation_tasks DROP COLUMN device_id')
            conn.execute('ALTER TABLE backend_operation_tasks ADD CONSTRAINT\n                backend_operation_tasks_operation_id_node_key_protocol_key UNIQUE(operation_id,node_key,protocol)')
            conn.execute('ALTER TABLE backend_config_issuances DROP COLUMN device_id')
            conn.execute('ALTER TABLE backend_config_issuances DROP COLUMN device_revision')
            conn.execute('ALTER TABLE backend_profiles DROP COLUMN created_at')
            conn.execute('ALTER TABLE backend_traffic_usage DROP COLUMN period_month')
            conn.execute('DELETE FROM backend_devices')
        migrate(self.db)
        with self.db.connect() as conn:
            after = conn.execute('SELECT * FROM backend_operation_tasks WHERE id=?',(task['id'],)).fetchone()
            self.assertEqual(after['intent_json'],task['intent_json'])
            self.assertEqual(after['id'],task['id'])
            self.assertTrue(after['device_id'])
            device = conn.execute('SELECT * FROM backend_devices WHERE id=?',(after['device_id'],)).fetchone()
            self.assertEqual(device['runtime_name'],'peer')

    def revision(self, number, name, upgrade, minimum_reader=1):
        path = Path(self.tmp.name)/(name+'.py')
        path.write_text(name)
        return Revision(number,name,SimpleNamespace(__file__=str(path),upgrade=upgrade),minimum_reader)

    def test_failed_pending_batch_rolls_back_schema_data_and_journal(self):
        migrate(self.db)
        def first(conn):
            conn.execute('CREATE TABLE migration_marker(id INTEGER PRIMARY KEY)')
            conn.execute("INSERT INTO schema_meta VALUES ('migration_marker','unchanged')")
        def fail(conn):
            conn.execute('CREATE TABLE migration_failed_marker(id INTEGER PRIMARY KEY)')
            raise RuntimeError('private diagnostic must not appear in error')
        revisions = (*REVISIONS,self.revision(len(REVISIONS)+1,'first',first),self.revision(len(REVISIONS)+2,'failed',fail))
        with self.assertRaisesRegex(MigrationError,f'revision={len(REVISIONS)+2}; transaction rolled back') as error:
            migrate(self.db,revisions=revisions)
        self.assertNotIn('private',str(error.exception))
        with self.db.connect() as conn:
            self.assertIsNone(conn.execute("SELECT to_regclass('migration_marker') AS name").fetchone()['name'])
            self.assertIsNone(conn.execute("SELECT to_regclass('migration_failed_marker') AS name").fetchone()['name'])
            self.assertIsNone(conn.execute("SELECT value FROM schema_meta WHERE key='migration_marker'").fetchone())
        self.assertEqual(check_schema(self.db)['current_revision'],len(REVISIONS))

    def test_concurrent_migrators_apply_each_revision_once(self):
        start = threading.Barrier(2)
        def run():
            start.wait(timeout=5)
            return migrate(self.db)['applied']
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _:run(),range(2)))
        self.assertEqual(sorted(results),[[],[r.number for r in REVISIONS]])
        self.assertEqual(check_schema(self.db)['current_revision'],len(REVISIONS))

    def test_immutable_history_rejects_changed_checksum_or_missing_revision(self):
        migrate(self.db)
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_schema_revisions SET checksum='changed' WHERE revision=1")
        with self.assertRaisesRegex(MigrationError,'migration_history_mismatch'):
            migrate(self.db)
        with self.assertRaisesRegex(MigrationError,'migration_history_mismatch'):
            check_schema(self.db)
        with self.db.transaction() as conn:
            conn.execute('DELETE FROM backend_schema_revisions WHERE revision=1')
        with self.assertRaisesRegex(MigrationError,'migration_history_invalid'):
            check_schema(self.db)

    def test_compatible_readers_can_rollback_application_but_cannot_migrate_backwards(self):
        migrate(self.db)
        future = self.revision(len(REVISIONS)+1,'additive',lambda conn:conn.execute('CREATE TABLE future_field(id INTEGER)'),1)
        migrate(self.db,revisions=(*REVISIONS,future))
        self.assertTrue(check_schema(self.db)['ready'])
        with self.assertRaisesRegex(MigrationError,'database_newer_than_migrator'):
            migrate(self.db)
        with self.db.transaction() as conn:
            conn.execute('UPDATE backend_schema_revisions SET minimum_reader=? WHERE revision=?',(len(REVISIONS)+1,len(REVISIONS)+1))
        with self.assertRaisesRegex(MigrationError,'database_reader_incompatible'):
            check_schema(self.db)

    def test_missing_schema_readiness_is_read_only(self):
        with self.assertRaisesRegex(MigrationError,'database_migration_required'):
            check_schema(self.db)
        with self.db.connect() as conn:
            self.assertIsNone(conn.execute("SELECT to_regclass('backend_schema_revisions') AS name").fetchone()['name'])

    def test_temporary_registry_lifecycle_and_restore_guard_on_postgres(self):
        migrate(self.db)
        from backend.admin_cli import bootstrap_admin
        from backend.identity_repository import SQLIdentityRepository
        from backend.authorization import Actor,Principal,PrincipalKind,ADMIN_PERMISSIONS
        from backend.temporary_configs import TemporaryConfigService
        from backend.backups import BackupService,TABLES,CLEAR
        from datetime import timedelta
        import json
        admin = bootstrap_admin(SQLIdentityRepository(self.db),101,self.db)
        actor = Actor(Principal('workstation-test',PrincipalKind.ACCOUNT,ADMIN_PERMISSIONS,admin.id),admin)
        with self.db.transaction() as conn:
            conn.execute('''INSERT INTO backend_nodes(key,title,region,enabled,protocols_json,
                xray_transports_json,desired_revision,applied_revision)
                VALUES ('lv1','Latvia #1','Europe',1,'["xray"]','["tcp"]',1,1)''')
        class Driver:
            calls = 0
            def node_action(self, identity, action, intent, recover=False):
                self.calls += 1
                if action=='temporary_revoke':
                    return {'revocation_mode':'new_connections_only'}
                return {'expires_at':(datetime.now(timezone.utc)+timedelta(seconds=intent['lease_seconds'])).isoformat(),
                        'revocation_mode':'new_connections_only','xray_uuid':intent['uuid']}
        driver = Driver()
        service = TemporaryConfigService(self.db,driver)
        key = str(uuid4())
        start = threading.Barrier(2)
        def create():
            start.wait(timeout=5)
            return service.create(actor,'lv1','xray','tcp',86400,key)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _:create(),range(2)))
        self.assertEqual(results[0],results[1])
        config = results[0]
        service.run_one()
        self.assertEqual(service.get(actor,config['id'])['status'],'active')
        with self.db.connect() as conn:
            self.assertTrue(BackupService.busy(conn))
        service.revoke(actor,config['id'])
        service.run_one()
        self.assertEqual(service.list(actor)['total'],0)
        self.assertEqual(driver.calls,2)
        with self.db.connect() as conn:
            self.assertFalse(BackupService.busy(conn))
            history = [dict(r) for r in conn.execute('SELECT * FROM backend_temporary_config_events').fetchall()]
        self.assertTrue(history)
        self.assertNotIn('xray_uuid',json.dumps(history))
        self.assertNotIn('backend_temporary_configs',TABLES)
        self.assertIn('backend_temporary_configs',CLEAR)
        self.assertNotIn('backend_temporary_config_events',CLEAR)
