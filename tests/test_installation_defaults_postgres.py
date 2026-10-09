"""Concurrent node/default commands against a disposable PostgreSQL database."""
from concurrent.futures import ThreadPoolExecutor
import os
import threading
import unittest
from uuid import uuid4

from backend.authorization import AccessDenied
from backend.installation_defaults import InstallationDefaults
from backend.nodes import NodeService
from tests import test_backups_postgres as fixture


@unittest.skipUnless(os.environ.get('NODE_PLANE_TEST_POSTGRES_DSN'), 'requires disposable PostgreSQL')
class InstallationDefaultsPostgresTests(unittest.TestCase):
    setUp = fixture.BackupsPostgresTests.setUp
    drop_schema = fixture.BackupsPostgresTests.drop_schema

    def test_concurrent_local_creation_only_claims_one_slot(self):
        from backend.admin_cli import bootstrap_admin
        from backend.authorization import Actor
        from backend.identity_repository import SQLIdentityRepository
        other_actor = Actor(self.actor.principal, bootstrap_admin(SQLIdentityRepository(self.db), 102, self.db))
        start = threading.Barrier(2)

        def create(key):
            start.wait(timeout=5)
            try:
                return NodeService(self.db).command(self.actor if key == 'local1' else other_actor, action='create', command_key=str(uuid4()),
                    values={'key': key, 'title': key, 'region': 'Europe', 'transport': 'local',
                        'protocols': ['awg'], 'settings': {'public_host': key + '.example'}})
            except AccessDenied as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(create, 'local1'), pool.submit(create, 'local2')
            results = [first.result(timeout=15), second.result(timeout=15)]
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertIn('local_node_exists', results)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) AS count FROM backend_nodes').fetchone()['count'], 1)
        self.assertFalse(InstallationDefaults(self.db).creation_options(self.actor)['local_available'])

    def test_template_allocation_serializes_different_admins_and_retired_codes(self):
        from backend.admin_cli import bootstrap_admin
        from backend.authorization import Actor
        from backend.identity_repository import SQLIdentityRepository
        other = bootstrap_admin(SQLIdentityRepository(self.db), 102, self.db)
        other_actor = Actor(self.actor.principal, other)
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_node_retirements VALUES ('lv1',?,'verified','test',NULL,0,'{}','2026-10-08')", (self.admin.id,))
        barrier = threading.Barrier(2)
        def create(actor):
            barrier.wait(timeout=5)
            return NodeService(self.db).command(actor, action='create', command_key=str(uuid4()),
                values={'template': 'lv', 'key': 'lv1', 'title': 'Latvia #1', 'region': 'Europe',
                    'transport': 'ssh', 'ssh_target': 'root@vpn.example', 'protocols': ['awg'],
                    'settings': {'public_host': 'vpn.example'}})
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(create, self.actor), pool.submit(create, other_actor)
            results = [first.result(timeout=15), second.result(timeout=15)]
        self.assertEqual({r['key'] for r in results}, {'lv2', 'lv3'})
        self.assertEqual({r['title'] for r in results}, {'Latvia #2', 'Latvia #3'})

    def test_concurrent_default_edits_do_not_overwrite_each_other(self):
        start = threading.Barrier(2)

        def update(preset):
            start.wait(timeout=5)
            try:
                return InstallationDefaults(self.db).update(self.actor,
                    {'protocols': ['awg'], 'xray_transports': [],
                        'settings': {'awg_i1_preset': preset, 'awg_port_mode': 'auto'}}, 1)
            except AccessDenied as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(update, 'dns'), pool.submit(update, 'quic')
            results = [first.result(timeout=15), second.result(timeout=15)]
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertIn('revision_conflict', results)
        self.assertEqual(InstallationDefaults(self.db).get(self.actor)['revision'], 2)

    def test_backup_restores_future_installation_defaults(self):
        service = InstallationDefaults(self.db)
        original = service.update(self.actor, {'protocols': ['awg'], 'xray_transports': [],
            'settings': {'awg_i1_preset': 'dns', 'awg_port_mode': 'auto'}}, 1)
        backup_id = self.service.create_snapshot()['backup_id']
        service.update(self.actor, {'protocols': ['awg'], 'xray_transports': [],
            'settings': {'awg_i1_preset': 'quic', 'awg_port_mode': 'auto'}}, 2)
        job = self.service.queue(self.actor, str(uuid4()), 'restore', backup_id,
            self.service.detail(self.actor, backup_id)['checksum'])
        while self.service.run_one():
            pass
        self.assertEqual(self.service.get(self.actor, job['id'])['status'], 'succeeded')
        self.assertEqual(service.get(self.actor), original)
