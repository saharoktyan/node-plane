"""Backup orchestration tests; PostgreSQL-specific statements are explicit fakes.

These exercise policy and transactions, not PostgreSQL integration semantics.
"""

import json
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
from backend.backups import BackupService
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

    def test_private_snapshot_deduplicates_and_excludes_credentials(self):
        backup_id, _ = self.snapshot()
        path = self.service._path(backup_id)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertNotIn(self.token, path.read_text())
        self.assertNotIn("backend_credentials", json.loads(path.read_text())["tables"])
        self.assertEqual(
            self.service.create_snapshot(),
            {"status": "duplicate", "backup_id": backup_id},
        )

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
        self.assertEqual(self.service.get(self.actor, job["id"])["status"], "succeeded")
        node = self.db.connection.execute("SELECT * FROM backend_nodes").fetchone()
        self.assertEqual(
            (node["enabled"], node["applied_revision"], node["desired_revision"]),
            (0, 0, 81),
        )
        restored = ProfileRepository(self.db).get(profile)
        self.assertEqual(restored["display_name"], "Alice")
        self.assertGreater(restored["desired_revision"], 91)
        self.assertEqual(self.identities.find_telegram_account(101).id, self.admin.id)

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
