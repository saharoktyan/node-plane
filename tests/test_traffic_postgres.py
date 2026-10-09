"""Optional real PostgreSQL regression, never a production database fixture.

Run this module separately with NODE_PLANE_TEST_POSTGRES_DSN pointing to a
disposable database; other test modules install an in-process psycopg double.
"""
import os
import unittest
from datetime import datetime, timezone
from uuid import uuid4
from unittest.mock import patch

from backend.traffic import TrafficService
from backend.devices import DeviceRepository
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
            DeviceRepository.create_schema(conn)
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

    def test_node_protocol_totals_use_postgres_upsert_and_idempotent_seed(self):
        now = datetime.now(timezone.utc)
        with self.db.transaction() as conn:
            conn.execute("ALTER TABLE backend_nodes ADD COLUMN protocols_json TEXT DEFAULT '[\"xray\",\"awg\"]'")
            conn.execute("INSERT INTO backend_devices(id,profile_id,display_name,runtime_name,status,created_at) VALUES ('device','owned','Phone','phone','active',?)", (now.isoformat(),))
            for protocol in ('xray','awg'):
                target = {'key':('owned','node',protocol,'device'), 'account_id':'admin'}
                observation = {'identity':protocol, 'epoch':'a'*64, 'uplink_bytes':1000, 'downlink_bytes':2000}
                self.service._record(conn,target,observation,now.isoformat())
                observation.update(uplink_bytes=1100,downlink_bytes=2200)
                from datetime import timedelta
                self.service._record(conn,target,observation,(now+timedelta(seconds=1)).isoformat())
        with patch.object(self.service, '_targets', return_value=[]):
            summary = self.service.node_summary('node')
            self.assertEqual(summary['total_bytes'],600)
            self.assertEqual(summary['status'],'current')
            with self.db.transaction() as conn:
                conn.execute('DELETE FROM backend_node_traffic')
            self.service.initialize_schema()
            self.service.initialize_schema()
            self.assertEqual(self.service.node_summary('node')['total_bytes'],600)
