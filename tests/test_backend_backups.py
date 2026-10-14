"""Backup orchestration tests; PostgreSQL-specific statements are explicit fakes.

These exercise policy and transactions, not PostgreSQL integration semantics.
"""

import json
import hashlib
import os
import tempfile
import unittest
from contextlib import contextmanager
from uuid import uuid4

from backend.authorization import (
    ADMIN_PERMISSIONS,
    AccessDenied,
    Actor,
    Principal,
    PrincipalKind,
)
from backend.backups import BackupService, encoded
from backend.devices import DeviceRepository, default_device
from backend.profiles import ProfileRepository

from tests import test_backend_http as http_fixture


class Connection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=()):
        if sql.startswith(("SET TRANSACTION", "LOCK TABLE")):
            return self.connection.execute("SELECT 1")
        return self.connection.execute(sql, params)


class BackupDatabase:
    def __init__(self, db):
        self.db = db

    @contextmanager
    def connect(self):
        with self.db.connect() as conn:
            yield Connection(conn)

    @contextmanager
    def transaction(self):
        with self.db.transaction() as conn:
            yield Connection(conn)


class TestService(BackupService):
    def _columns(self, conn, table):
        return [
            column[0]
            for column in conn.execute(f"SELECT * FROM {table} LIMIT 0").description
        ]


class BackendBackupsTests(unittest.TestCase):
    def setUp(self):
        http_fixture.BackendHTTPTests.setUp(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = TestService(BackupDatabase(self.db), self.temp.name)
        self.actor = Actor(
            Principal("test", PrincipalKind.SERVICE, ADMIN_PERMISSIONS), self.admin
        )

    def snapshot(self):
        result = self.service.create_snapshot()
        return result["backup_id"], self.service.detail(
            self.actor, result["backup_id"]
        )["checksum"]

    def test_export_import_is_portable_idempotent_and_does_not_restore(self):
        identity, checksum = self.snapshot()
        content = self.service.export_snapshot(self.actor, identity)
        with tempfile.TemporaryDirectory() as destination:
            target = TestService(BackupDatabase(self.db), destination)
            before = self.db.connection.execute('SELECT count(*) FROM backend_accounts').fetchone()[0]
            imported = target.import_snapshot(self.actor, content)
            self.assertEqual(imported['checksum'], checksum)
            self.assertEqual(target.import_snapshot(self.actor, content)['id'], imported['id'])
            self.assertEqual(target.export_snapshot(self.actor, imported['id']), content)
            self.assertEqual(target._path(imported['id']).stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.db.connection.execute('SELECT count(*) FROM backend_accounts').fetchone()[0], before)
            self.assertEqual(len(list(target.root.glob('*.json'))), 1)

    def test_import_rejects_corruption_without_creating_a_catalog_entry(self):
        identity, _ = self.snapshot()
        payload = json.loads(self.service.export_snapshot(self.actor, identity))
        payload['checksum'] = '0' * 64
        with tempfile.TemporaryDirectory() as destination:
            target = TestService(BackupDatabase(self.db), destination)
            with self.assertRaises(AccessDenied) as failure:
                target.import_snapshot(self.actor, encoded(payload))
            self.assertEqual(failure.exception.code, 'backup_invalid')
            self.assertEqual(list(target.root.glob('*.json')), [])

    def test_restore_on_new_controller_preserves_same_telegram_admin_and_profile_ownership(self):
        from dataclasses import replace
        profile_id = ProfileRepository(self.db).ensure_account_profile(self.admin.id)
        backup_id, checksum = self.snapshot()
        new_id = str(uuid4())
        self.db.connection.execute("INSERT INTO backend_accounts(id,role,status) VALUES (?,'admin','approved')", (new_id,))
        self.db.connection.execute("UPDATE backend_external_identities SET account_id=? WHERE provider='telegram' AND subject='101'", (new_id,))
        self.db.connection.commit()
        actor = Actor(self.actor.principal, replace(self.admin, id=new_id))
        job = self.service.queue(actor, str(uuid4()), 'restore', backup_id, checksum)
        self.assertTrue(self.service.run_one())
        self.assertTrue(self.service.run_one())
        self.assertEqual(self.service.get(actor, job['id'])['phase'], 'restored')
        self.assertEqual(self.identities.find_telegram_account(101).id, new_id)
        self.assertEqual(ProfileRepository(self.db).get(profile_id)['owner_account_id'], new_id)
        self.assertIsNone(self.db.connection.execute('SELECT id FROM backend_accounts WHERE id=?', (self.admin.id,)).fetchone())

    def test_http_import_and_export_validate_authentication_and_return_original_snapshot(self):
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from backend.http_api import create_app
        identity, _ = self.snapshot()
        content = self.service.export_snapshot(self.actor, identity)
        headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101'}
        with tempfile.TemporaryDirectory() as destination, patch.dict(os.environ, {'NODE_PLANE_SHARED_DIR': destination}), \
                patch.object(BackupService, '_columns', TestService._columns), TestClient(create_app(self.db)) as client:
            imported = client.post('/api/v1/system/backups/import', headers=headers, content=content)
            self.assertEqual(imported.status_code, 201, imported.text)
            path = f'/api/v1/system/backups/catalog/{imported.json()["id"]}/export'
            exported = client.get(path, headers=headers)
            self.assertEqual(exported.status_code, 200, exported.text)
            self.assertEqual(exported.content, content)
            self.assertEqual(exported.headers['Cache-Control'], 'no-store')
            self.assertIn('attachment', exported.headers['Content-Disposition'])
            self.assertEqual(client.get(path).status_code, 401)
            bad = client.post('/api/v1/system/backups/import', headers=headers, content=b'{}')
            self.assertEqual(bad.status_code, 409, bad.text)

    def test_delete_requires_original_checksum_and_rejects_traversal(self):
        identity, checksum = self.snapshot()
        with self.assertRaises(AccessDenied) as failure:
            self.service.delete(self.actor, identity, "0" * 64)
        self.assertEqual(failure.exception.code, "backup_changed")
        self.assertTrue(self.service._path(identity).exists())
        self.assertEqual(self.service.delete(self.actor, identity, checksum), {"deleted_backup_id":identity})
        self.assertFalse(self.service._path(identity).exists())
        with self.assertRaises(AccessDenied):
            self.service.delete(self.actor, "../../outside", checksum)

    def test_delete_rejects_backup_referenced_by_blocked_restore(self):
        identity, checksum = self.snapshot()
        job = self.service.queue(self.actor, str(uuid4()), "restore", identity, checksum)
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_backup_jobs SET status='blocked' WHERE id=?", (job["id"],))
        with self.assertRaises(AccessDenied) as failure:
            self.service.delete(self.actor, identity, checksum)
        self.assertEqual(failure.exception.code,"backup_in_use")
        self.assertTrue(self.service._path(identity).exists())

    def test_overview_reports_actual_nonzero_file_sizes_after_profile_changes(self):
        first_id, _ = self.snapshot()
        first_size = self.service._path(first_id).stat().st_size
        self.assertGreater(first_size, 0)
        self.assertEqual(self.service.overview(self.actor)['size_bytes'], first_size)
        ProfileRepository(self.db).create_profile(runtime_name='size_test', display_name='Size Test')
        second_id, _ = self.snapshot()
        self.assertNotEqual(first_id, second_id)
        total = first_size + self.service._path(second_id).stat().st_size
        overview = self.service.overview(self.actor)
        self.assertEqual(overview['count'], 2)
        self.assertEqual(overview['size_bytes'], total)
        self.assertEqual(self.service.catalog(self.actor, limit=1)['total_size_bytes'], total)

    def test_manual_snapshots_are_distinct_and_scheduled_snapshots_deduplicate(self):
        backup_id, _ = self.snapshot()
        path = self.service._path(backup_id)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertNotIn(self.token, path.read_text())
        self.assertNotIn("backend_credentials", json.loads(path.read_text())["tables"])
        manual = self.service.create_snapshot()
        self.assertEqual(manual['status'], 'success')
        self.assertNotEqual(manual['backup_id'], backup_id)
        automatic = self.service.create_snapshot('scheduled')
        self.assertEqual(self.service.create_snapshot('scheduled'), {'status':'duplicate','backup_id':automatic['backup_id']})

    def device_profile(self):
        profile_id = ProfileRepository(self.db).create_profile(runtime_name='existing_awg', display_name='Alice')
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_nodes(key,title,region,protocols_json) VALUES ('n1','Node','Europe','[\"awg\"]')")
            conn.execute("INSERT INTO backend_grants(profile_id,node_key,protocol) VALUES (?,'n1','awg')", (profile_id,))
            profile = conn.execute('SELECT * FROM backend_profiles WHERE id=?', (profile_id,)).fetchone()
            DeviceRepository.ensure_default(conn, profile)
        return profile_id

    def test_retention_only_prunes_automatic_backups_and_manual_names_survive_export(self):
        self.service.preferences(self.actor, {'keep_count': 5})
        manual = [self.service.create_snapshot(name='Manual '+str(index))['backup_id'] for index in range(3)]
        for index in range(8):
            with self.db.transaction() as conn:
                conn.execute("INSERT INTO backend_system_settings(key,value) VALUES ('test_change',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(index),))
            self.service.create_snapshot('scheduled')
        catalog = self.service.catalog(self.actor, limit=100)['items']
        self.assertEqual(len([item for item in catalog if item['kind']=='automatic']), 5)
        self.assertEqual({item['id'] for item in catalog if item['kind']=='manual'}, set(manual))
        self.assertEqual(self.service.detail(self.actor, manual[0])['name'], 'Manual 0')
        payload = self.service.decode_snapshot(self.service.export_snapshot(self.actor,manual[0]))
        self.assertEqual(payload['name'], 'Manual 0')
        self.service.delete(self.actor,manual[0],payload['checksum'])
        self.assertFalse(self.service._path(manual[0]).exists())

    def test_named_backup_command_is_idempotent_and_worker_keeps_label(self):
        key=str(uuid4())
        job=self.service.queue(self.actor,key,'create',name='Before changes')
        self.assertEqual(self.service.queue(self.actor,key,'create',name='Before changes')['id'],job['id'])
        with self.assertRaises(AccessDenied) as failure:
            self.service.queue(self.actor,key,'create',name='Other name')
        self.assertEqual(failure.exception.code,'idempotency_conflict')
        self.service.run_one()
        result=self.service.get(self.actor,job['id'])['result']
        self.assertEqual(self.service.detail(self.actor,result['backup_id'])['name'],'Before changes')

    def test_device_snapshot_preserves_identity_name_and_tombstone(self):
        profile_id = self.device_profile()
        self.db.connection.execute("UPDATE backend_devices SET display_name='Phone',status='retired',revision=3")
        backup_id, _ = self.snapshot()
        payload = self.service._load(backup_id)
        self.assertEqual(payload['format'], 'node-plane-backend-v3')
        saved = payload['tables']['backend_devices']['rows'][0]
        self.assertEqual(saved['profile_id'], profile_id)
        self.assertEqual(saved['runtime_name'], 'existing_awg')
        self.assertEqual((saved['display_name'], saved['status'], saved['revision']), ('Phone', 'retired', 3))
        self.assertTrue(self.service.detail(self.actor, backup_id)['compatible'])

    def test_v2_snapshot_gains_regions_without_adopting_policy_access(self):
        self.device_profile()
        backup_id, _ = self.snapshot()
        path = self.service._path(backup_id)
        payload = json.loads(path.read_bytes())
        from backend.grant_policies import TABLE_COLUMNS
        for table in TABLE_COLUMNS:
            del payload['tables'][table]
        payload['format'] = 'node-plane-backend-v2'
        payload['checksum'] = hashlib.sha256(encoded(payload['tables'])).hexdigest()
        path.write_bytes(encoded(payload))
        converted = self.service._load(backup_id)
        self.assertEqual(converted['checksum'], payload['checksum'])
        self.assertEqual(len(converted['tables']['backend_node_regions']['rows']), 1)
        self.assertEqual(converted['tables']['backend_grant_policies']['rows'], [])
        self.assertEqual(converted['tables']['backend_devices'], payload['tables']['backend_devices'])
        self.assertTrue(self.service.detail(self.actor, backup_id)['compatible'])

    def test_legacy_snapshot_conversion_keeps_original_checksum_and_peer(self):
        profile_id = self.device_profile()
        backup_id, _ = self.snapshot()
        path = self.service._path(backup_id)
        payload = json.loads(path.read_bytes())
        payload['format'] = 'node-plane-backend-v1'
        del payload['tables']['backend_devices']
        from backend.grant_policies import TABLE_COLUMNS
        for table in TABLE_COLUMNS:
            del payload['tables'][table]
        payload['checksum'] = hashlib.sha256(encoded(payload['tables'])).hexdigest()
        path.write_bytes(encoded(payload))
        converted = self.service._load(backup_id)
        device = converted['tables']['backend_devices']['rows'][0]
        self.assertEqual(device['profile_id'], profile_id)
        self.assertEqual(device['runtime_name'], 'existing_awg')
        self.assertEqual(converted['checksum'], payload['checksum'])
        self.assertEqual(self.service._load(backup_id)['tables']['backend_devices'], converted['tables']['backend_devices'])
        self.assertTrue(self.service.detail(self.actor, backup_id)['compatible'])
        profile = payload['tables']['backend_profiles']['rows'][0]
        self.assertEqual(device['id'], default_device(profile)['id'])
        # A tampered old snapshot must fail before device conversion.
        payload['tables']['backend_profiles']['rows'][0]['runtime_name'] = 'tampered'
        path.write_bytes(encoded(payload))
        with self.assertRaises(AccessDenied) as error:
            self.service._load(backup_id)
        self.assertEqual(error.exception.code, 'backup_invalid')

    def test_queue_is_idempotent_and_rejects_payload_reuse(self):
        key = str(uuid4())
        first = self.service.queue(self.actor, key, "create")
        self.assertEqual(
            first["id"], self.service.queue(self.actor, key, "create")["id"]
        )
        with self.assertRaises(AccessDenied) as error:
            self.service.queue(self.actor, key, "restore", str(uuid4()), "a" * 64)
        self.assertEqual(error.exception.code, "idempotency_conflict")
        self.assertTrue(self.service.run_one())
        self.assertEqual(
            self.service.get(self.actor, first["id"])["status"], "succeeded"
        )

    def test_failed_installation_history_stops_blocking_after_explicit_retirement(self):
        backup_id, checksum = self.snapshot()
        with self.db.transaction() as conn:
            conn.execute("""INSERT INTO backend_agent_rollouts
                (id,node_key,actor_account_id,command_key,intent_json,status)
                VALUES (?,'retired',?,?,'{}','blocked')""",
                (str(uuid4()), self.admin.id, str(uuid4())))
        # Missing metadata alone is not proof of retirement. Explicit
        # abandonment is terminal but does not assert remote cleanup.
        with self.assertRaises(AccessDenied) as error:
            self.service.queue(self.actor, str(uuid4()), 'restore', backup_id, checksum)
        self.assertEqual(error.exception.code, 'maintenance_busy')
        with self.db.transaction() as conn:
            conn.execute("""INSERT INTO backend_node_retirements
                VALUES ('retired',?,'registry_only','unverified',NULL,0,NULL,'2026-10-06')""", (self.admin.id,))
        job = self.service.queue(self.actor, str(uuid4()), 'restore', backup_id, checksum)
        self.assertEqual(job['status'], 'awaiting_executor')
        self.assertTrue(self.service.run_one())
        self.assertTrue(self.service.run_one())
        self.assertEqual(self.service.get(self.actor, job['id'])['status'], 'succeeded')

    def test_failed_installation_on_existing_node_still_blocks_restore(self):
        backup_id, checksum = self.snapshot()
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_nodes(key,title,region,protocols_json) VALUES ('test','Test','EU','[]')")
            conn.execute("""INSERT INTO backend_agent_rollouts
                (id,node_key,actor_account_id,command_key,intent_json,status)
                VALUES (?,'test',?,?,'{}','blocked')""",
                (str(uuid4()), self.admin.id, str(uuid4())))
        with self.assertRaises(AccessDenied) as error:
            self.service.queue(self.actor, str(uuid4()), 'restore', backup_id, checksum)
        self.assertEqual(error.exception.code, 'maintenance_busy')

    def test_corrupt_and_traversal_snapshots_are_rejected(self):
        backup_id, _ = self.snapshot()
        path = self.service._path(backup_id)
        value = json.loads(path.read_text())
        value["tables"]["backend_accounts"]["rows"][0]["role"] = "member"
        path.write_text(json.dumps(value))
        with self.assertRaises(AccessDenied) as error:
            self.service.detail(self.actor, backup_id)
        self.assertEqual(error.exception.code, "backup_invalid")
        self.assertEqual(self.service.catalog(self.actor)["total"], 0)
        with self.assertRaises(AccessDenied):
            self.service._path("../secret")

    def test_restore_preserves_admin_and_fences_node_and_profile_revisions(self):
        profile = ProfileRepository(self.db).create_profile(
            runtime_name="alice", display_name="Alice"
        )
        self.db.connection.execute(
            "INSERT INTO backend_nodes(key,title,region,protocols_json) VALUES ('test','Test','EU','[]')"
        )
        self.db.connection.commit()
        backup_id, checksum = self.snapshot()
        self.db.connection.execute(
            "UPDATE backend_nodes SET desired_revision=80,applied_revision=80"
        )
        self.db.connection.execute(
            "UPDATE backend_profiles SET desired_revision=90,display_name='Changed'"
        )
        self.db.connection.commit()
        job = self.service.queue(
            self.actor, str(uuid4()), "restore", backup_id, checksum
        )
        self.assertTrue(self.service.run_one())
        self.assertEqual(self.service.get(self.actor, job["id"])["phase"], "revoking")
        self.assertTrue(self.service.run_one())
        progress = self.service.get(self.actor, job["id"])
        self.assertEqual(progress["status"], "running")
        self.assertEqual(progress["phase"], "reconnecting")
        self.assertEqual(progress["result"]["nodes"], [{"node_key": "test", "status": "pending"}])
        node = self.db.connection.execute("SELECT * FROM backend_nodes").fetchone()
        self.assertEqual(
            (node["enabled"], node["applied_revision"], node["desired_revision"]),
            (0, 0, 81),
        )
        restored = ProfileRepository(self.db).get(profile)
        self.assertEqual(restored["display_name"], "Alice")
        self.assertGreater(restored["desired_revision"], 91)
        self.assertEqual(self.identities.find_telegram_account(101).id, self.admin.id)

    def test_restored_nodes_reconnect_and_partial_result_survives_worker_restart(self):
        from uuid import UUID, uuid5
        from backend.node_bootstrap import NodeBootstrapService
        from unittest.mock import patch
        with self.db.transaction() as conn:
            for key in ('first', 'second'):
                conn.execute("INSERT INTO backend_nodes(key,title,region,protocols_json) VALUES (?,?, 'EU','[]')", (key, key))
        backup_id, checksum = self.snapshot()
        parent = self.service.queue(self.actor, str(uuid4()), 'restore', backup_id, checksum)
        self.service.run_one()
        self.service.run_one()
        self.assertEqual(self.service.get(self.actor, parent['id'])['phase'], 'reconnecting')
        # Simulate a committed child whose parent link was lost in a worker crash.
        children = {}
        with self.db.transaction() as conn:
            for key in ('first', 'second'):
                identity = str(uuid4())
                children[key] = identity
                conn.execute("""INSERT INTO backend_node_bootstraps
                    (id,node_key,actor_id,command_key,revision,phase,status)
                    VALUES (?,?,?,?,1,'protocols','running')""",
                    (identity, key, self.actor.account.id, str(uuid5(UUID(parent['id']), key))))
        with patch.object(NodeBootstrapService, 'queue') as queue:
            self.assertTrue(self.service.run_one())
            self.assertTrue(self.service.run_one())
            queue.assert_not_called()
        self.assertFalse(self.service.run_one())
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_node_bootstraps SET status='succeeded' WHERE id=?", (children['first'],))
            conn.execute("UPDATE backend_node_bootstraps SET status='blocked',error_code='node_agent_unavailable' WHERE id=?", (children['second'],))
        restarted = TestService(BackupDatabase(self.db), self.temp.name)
        self.assertTrue(restarted.run_one())
        final = restarted.get(self.actor, parent['id'])
        self.assertEqual(final['status'], 'succeeded')
        self.assertEqual(final['result']['status'], 'partial')
        statuses = {item['node_key']: item for item in final['result']['nodes']}
        self.assertEqual(statuses['first']['status'], 'succeeded')
        self.assertEqual(statuses['second']['error_code'], 'node_agent_unavailable')
        self.assertFalse(restarted.run_one())

    def test_restore_blocks_mutations_but_keeps_progress_readable(self):
        backup_id, checksum = self.snapshot()
        job = self.service.queue(
            self.actor, str(uuid4()), "restore", backup_id, checksum
        )
        headers = {**self.headers, "X-Node-Plane-Telegram-User-ID": "101"}
        result = self.client.post(
            "/api/v1/integrations/telegram/identities/resolve",
            headers=headers,
            json={"telegram_user_id": 102},
        )
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()["error"]["code"], "restore_in_progress")
        self.assertEqual(
            self.client.get(
                f"/api/v1/system/backups/jobs/{job['id']}", headers=headers
            ).status_code,
            200,
        )

    def test_failed_revocation_prevents_restore(self):
        profile = ProfileRepository(self.db).create_profile(
            runtime_name="alice", display_name="Alice"
        )
        self.db.connection.execute(
            "INSERT INTO backend_nodes(key,title,region,protocols_json) VALUES ('test','Test','EU','[\"xray\"]')"
        )
        self.db.connection.execute(
            "INSERT INTO backend_grants VALUES (?, 'test','xray')", (profile,)
        )
        self.db.connection.commit()
        backup_id, checksum = self.snapshot()
        job = self.service.queue(
            self.actor, str(uuid4()), "restore", backup_id, checksum
        )
        self.service.run_one()
        self.db.connection.execute("UPDATE backend_operations SET status='blocked'")
        self.db.connection.commit()
        self.service.run_one()
        result = self.service.get(self.actor, job["id"])
        self.assertEqual(result["result"]["code"], "backup_revocations_failed")
        self.assertTrue(ProfileRepository(self.db).get(profile)["frozen"])
        self.assertEqual(
            self.db.connection.execute("SELECT enabled FROM backend_nodes").fetchone()[
                0
            ],
            1,
        )

    def test_preferences_validate_types_and_scheduler_does_not_repeat(self):
        with self.assertRaises(AccessDenied):
            self.service.preferences(self.actor, {"interval_hours": True})
        self.service.preferences(
            self.actor, {"enabled": True, "interval_hours": 6, "keep_count": 5}
        )
        self.service.scheduled()
        self.service.scheduled()
        self.assertEqual(self.service.catalog(self.actor)["total"], 1)
