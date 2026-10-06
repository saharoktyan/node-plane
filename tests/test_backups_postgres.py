"""Real restore regressions, run separately against disposable PostgreSQL.

Other test modules install a psycopg double; this module must run in its own
process with NODE_PLANE_TEST_POSTGRES_DSN set to a disposable database.
"""
import os
import tempfile
import unittest
from uuid import uuid4

from backend.admin_cli import bootstrap_admin
from backend.authorization import ADMIN_PERMISSIONS, Actor, Principal, PrincipalKind
from backend.backups import BackupService
from backend.identity_repository import SQLIdentityRepository
from backend.credentials import CredentialService
from backend.profiles import ProfileRepository
from backend.profile_commands import ProfileCommands
from backend.access_requests import AccessRequestService
from backend.system_settings import SystemSettingsService
from backend.accounts import AccountService
from backend.nodes import NodeService
from backend.node_settings import NodeSettingsService
from backend.config_issuance import ConfigIssuanceService
from backend.agent_rollout import AgentRolloutService
from db.postgres_db import PostgresDB


@unittest.skipUnless(os.environ.get('NODE_PLANE_TEST_POSTGRES_DSN'), 'requires disposable PostgreSQL')
class BackupsPostgresTests(unittest.TestCase):
    def setUp(self):
        from psycopg.conninfo import make_conninfo
        self.schema = 'backup_test_' + uuid4().hex
        self.base = PostgresDB(os.environ['NODE_PLANE_TEST_POSTGRES_DSN'])
        with self.base.transaction() as conn:
            conn.execute(f'CREATE SCHEMA {self.schema}')
        self.addCleanup(self.drop_schema)
        # Set search_path at connection creation, so snapshot isolation is the
        # first statement in its transaction, exactly as in production.
        self.db = PostgresDB(make_conninfo(self.base.dsn, options=f'-c search_path={self.schema}'))
        identities = SQLIdentityRepository(self.db)
        for service in (identities, CredentialService(self.db), ProfileRepository(self.db),
                        ProfileCommands(self.db), AccessRequestService(self.db),
                        SystemSettingsService(self.db), AccountService(self.db),
                        NodeService(self.db), NodeSettingsService(self.db),
                        ConfigIssuanceService(self.db), AgentRolloutService(self.db)):
            service.initialize_schema()
        self.admin = bootstrap_admin(identities, 101, self.db)
        self.actor = Actor(Principal('test', PrincipalKind.SERVICE, ADMIN_PERMISSIONS), self.admin)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.service = BackupService(self.db, directory.name)

    def drop_schema(self):
        with self.base.transaction() as conn:
            conn.execute(f'DROP SCHEMA {self.schema} CASCADE')

    def test_restore_after_verified_node_removal_preserves_admin_and_restores_profile(self):
        profile = ProfileRepository(self.db).create_profile(runtime_name='alice', display_name='Alice')
        backup_id = self.service.create_snapshot()['backup_id']
        checksum = self.service.detail(self.actor, backup_id)['checksum']
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_profiles SET display_name='Changed' WHERE id=?", (profile,))
            conn.execute("""INSERT INTO backend_node_retirements VALUES
                ('retired',?,'verified','host verified',NULL,0,'{}','2026-10-06')""", (self.admin.id,))
            for _ in range(3):
                conn.execute("""INSERT INTO backend_agent_rollouts
                    (id,node_key,actor_account_id,command_key,intent_json,status)
                    VALUES (?,'retired',?,?,'{}','blocked')""",
                    (str(uuid4()), self.admin.id, str(uuid4())))
        job = self.service.queue(self.actor, str(uuid4()), 'restore', backup_id, checksum)
        self.assertTrue(self.service.run_one())
        self.assertEqual(self.service.get(self.actor, job['id'])['phase'], 'revoking')
        self.assertTrue(self.service.run_one())
        self.assertEqual(self.service.get(self.actor, job['id'])['status'], 'succeeded')
        self.assertEqual(ProfileRepository(self.db).get(profile)['display_name'], 'Alice')
        self.assertEqual(SQLIdentityRepository(self.db).find_telegram_account(101).id, self.admin.id)
