"""Optional real PostgreSQL regression, never a production database fixture.

Run this module separately with NODE_PLANE_TEST_POSTGRES_DSN pointing to a
disposable database; other test modules install an in-process psycopg double.
"""
import os
import unittest
from datetime import datetime, timezone
from uuid import uuid4

from backend.traffic import TrafficService
from db.postgres_db import PostgresDB


@unittest.skipUnless(os.environ.get('NODE_PLANE_TEST_POSTGRES_DSN'), 'requires disposable PostgreSQL')
class TrafficPostgresTests(unittest.TestCase):
    def setUp(self):
        self.schema = 'traffic_test_' + uuid4().hex
        self.base = PostgresDB(os.environ['NODE_PLANE_TEST_POSTGRES_DSN'])
        with self.base.connect() as conn:
            conn.execute(f'CREATE SCHEMA {self.schema}')
        self.addCleanup(self.drop_schema)
        schema = self.schema

        class ScopedDB(PostgresDB):
            def _open(inner):
                conn = super()._open()
                conn.execute(f'SET search_path TO {schema}')
                return conn

        self.db = ScopedDB(self.base.dsn)
        with self.db.transaction() as conn:
            conn.execute('CREATE TABLE backend_accounts (id TEXT PRIMARY KEY)')
            conn.execute('CREATE TABLE backend_nodes (key TEXT PRIMARY KEY)')
            conn.execute('CREATE TABLE backend_profiles (id TEXT PRIMARY KEY, owner_account_id TEXT)')
            conn.execute('CREATE TABLE backend_system_settings (key TEXT PRIMARY KEY, value TEXT)')
            conn.execute("INSERT INTO backend_accounts VALUES ('admin')")
            conn.execute("INSERT INTO backend_nodes VALUES ('node')")
            conn.execute("INSERT INTO backend_profiles VALUES ('owned','admin'), ('ownerless',NULL)")
            conn.execute("INSERT INTO backend_system_settings VALUES ('traffic_enabled','true')")
        self.service = TrafficService(self.db)
        self.service.initialize_schema()

    def drop_schema(self):
        with self.base.connect() as conn:
            conn.execute(f'DROP SCHEMA {self.schema} CASCADE')

    def test_fresh_owned_and_ownerless_profiles_return_waiting(self):
        for owner, profile in [('admin', 'owned'), (None, 'ownerless')]:
            result = self.service.summary(owner, profile)
            self.assertEqual(result['status'], 'waiting')
            self.assertEqual(result['items'], [])
            self.assertEqual(result['nodes'], [])

    def test_null_owner_and_wrong_owner_are_isolated(self):
        now = datetime.now(timezone.utc)
        with self.db.transaction() as conn:
            for profile, owner in [('owned', 'admin'), ('ownerless', None)]:
                conn.execute('''INSERT INTO backend_traffic_usage
                    (profile_id,account_id,node_key,protocol,uplink_bytes,downlink_bytes,
                     tracked_since,last_sample_at,status,period_month)
                    VALUES (?,?,'node','awg',100,200,?,?,'current',?)''',
                    (profile, owner, now.isoformat(), now.isoformat(), now.strftime('%Y-%m')))
        for owner, profile in [('admin', 'owned'), (None, 'ownerless')]:
            self.assertEqual(self.service.summary(owner, profile)['items'][0]['uplink_bytes'], 100)
        for owner, profile in [(None, 'owned'), ('admin', 'ownerless'), ('different', 'owned')]:
            self.assertEqual(self.service.summary(owner, profile)['items'], [])
