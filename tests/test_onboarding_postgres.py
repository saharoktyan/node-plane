"""Check guide selection against the production SQL engine."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from backend.onboarding import progress
from backend.profiles import ProfileRepository
from tests import test_backups_postgres as fixture


@unittest.skipUnless(os.environ.get('NODE_PLANE_TEST_POSTGRES_DSN'), 'requires disposable PostgreSQL')
class OnboardingPostgresTests(unittest.TestCase):
    setUp = fixture.BackupsPostgresTests.setUp
    drop_schema = fixture.BackupsPostgresTests.drop_schema

    def test_guide_counts_awg_issuance_on_other_node(self):
        profile_id = ProfileRepository(self.db).ensure_account_profile(self.admin.id)
        profile = ProfileRepository(self.db).get(profile_id)
        timestamp = datetime.now(timezone.utc)
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_system_settings(key,value) VALUES ('installation_defaults','{}')")
            for key in ('a-local', 'z-awg'):
                conn.execute("INSERT INTO backend_nodes(key,title,region,enabled,protocols_json,xray_transports_json,desired_revision,applied_revision,settings_json) VALUES (?,?,?,1,?, '[]',1,1,'{}')",
                    (key,key,'Europe','["awg"]'))
                conn.execute("INSERT INTO backend_grants(profile_id,node_key,protocol) VALUES (?,?,'awg')", (profile_id,key))
        self.assertEqual(progress(self.db,self.actor)['next_step'], 'config')
        with self.db.transaction() as conn:
            conn.execute("""INSERT INTO backend_config_issuances(id,actor_account_id,command_key,
                profile_id,node_key,protocol,transport,profile_revision,node_revision,status,created_at,expires_at)
                VALUES (?,?,?,?,'z-awg','awg','vpn',?,1,'succeeded',?,?)""",
                (str(uuid4()),self.admin.id,str(uuid4()),profile_id,profile['desired_revision'],
                 timestamp.isoformat(),(timestamp+timedelta(minutes=10)).isoformat()))
        value = progress(self.db,self.actor)
        self.assertEqual(value['node_key'], 'z-awg')
        self.assertIsNone(value['next_step'])
        self.assertFalse(value['completed'])
