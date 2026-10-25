"""Controller maintenance policy tests; host effects are explicit fakes."""

import tempfile
import unittest
from unittest.mock import Mock
from uuid import uuid4

from backend.authorization import AccessDenied, Actor
from backend.maintenance_gate import admit
from backend.profiles import ProfileRepository
from backend.system_cleanup import SystemCleanupService

from tests import test_backend_backups as backup_fixture
from tests import test_backend_http as fixture


class SystemCleanupTests(unittest.TestCase):
    def setUp(self):
        fixture.BackendHTTPTests.setUp(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = Mock()
        self.host.deployment.return_value = {
            "base_dir": "/opt/node-plane",
            "shared_dir": "/opt/node-plane/shared",
            "postgres_container": None,
            "units": [],
        }
        self.host.prepare_uninstall.return_value = {
            "unit": "fake-unit",
            "script": "/not-executed",
        }
        self.backups = backup_fixture.TestService(
            backup_fixture.BackupDatabase(self.db), self.temp.name
        )
        self.service = SystemCleanupService(
            self.db, host=self.host, backups=self.backups
        )
        self.actor = Actor(
            self.credentials.authenticate(self.headers["Authorization"]), self.admin
        )

    def queue(self, action="reset", nodes=False):
        plan = self.service.plan(self.actor, action, nodes)
        return self.service.queue(
            self.actor, plan["id"], plan["confirmation_phrase"], str(uuid4())
        )

    def advance(self, job, target, limit=10):
        for _ in range(limit):
            if self.service.get(self.actor, job["id"])["status"] == target:
                return
            self.service.run_one()
        self.fail(self.service.get(self.actor, job["id"]))

    def test_phrase_plan_fingerprint_and_single_use(self):
        plan = self.service.plan(self.actor, "reset", False)
        with self.assertRaises(AccessDenied) as error:
            self.service.queue(self.actor, plan["id"], "yes", str(uuid4()))
        self.assertEqual(error.exception.code, "confirmation_mismatch")
        key = str(uuid4())
        first = self.service.queue(
            self.actor, plan["id"], plan["confirmation_phrase"], key
        )
        self.assertEqual(
            first["id"],
            self.service.queue(
                self.actor, plan["id"], plan["confirmation_phrase"], key
            )["id"],
        )
        self.assertEqual(
            first["id"],
            self.service.queue(
                self.actor, plan["id"], plan["confirmation_phrase"], str(uuid4())
            )["id"],
        )
        with self.db.transaction() as conn:
            with self.assertRaises(AccessDenied) as error:
                admit(conn)
            self.assertEqual(error.exception.code, "system_cleanup_in_progress")

    def test_stale_confirmation_does_not_queue(self):
        plan = self.service.plan(self.actor, "reset", False)
        ProfileRepository(self.db).create_profile(
            runtime_name="alice", display_name="Alice"
        )
        with self.assertRaises(AccessDenied) as error:
            self.service.queue(
                self.actor, plan["id"], plan["confirmation_phrase"], str(uuid4())
            )
        self.assertEqual(error.exception.code, "cleanup_plan_changed")

    def test_reset_retains_operator_identity_and_active_client_credential(self):
        ProfileRepository(self.db).create_profile(
            runtime_name="alice", display_name="Alice"
        )
        operator_profile = ProfileRepository(self.db).create_profile(
            runtime_name="operator", display_name="Operator", owner_account_id=self.admin.id)
        from backend.devices import DeviceRepository
        with self.db.transaction() as conn:
            profile = conn.execute('SELECT * FROM backend_profiles WHERE id=?', (operator_profile,)).fetchone()
            device_id = DeviceRepository.ensure_default(conn, profile)
        job = self.queue()
        self.advance(job, "succeeded")
        self.assertIsNotNone(ProfileRepository(self.db).get(operator_profile))
        with self.db.connect() as conn:
            self.assertIsNotNone(conn.execute('SELECT id FROM backend_devices WHERE id=?', (device_id,)).fetchone())
        self.assertEqual(self.identities.find_telegram_account(101).id, self.admin.id)
        self.assertEqual(
            self.credentials.authenticate(self.headers["Authorization"]).id,
            self.actor.principal.id,
        )
        with self.db.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM backend_profiles").fetchone()[0], 1
            )
            admit(conn)
        self.host.clear_local.assert_called_once()
        self.assertTrue(self.service.get(self.actor, job["id"])["backup_id"])

    def test_backup_failure_does_not_erase_profiles_and_can_abort(self):
        profile = ProfileRepository(self.db).create_profile(
            runtime_name="alice", display_name="Alice"
        )
        self.service.backups = Mock()
        self.service.backups.create_snapshot.side_effect = RuntimeError("secret output")
        job = self.queue()
        self.advance(job, "blocked")
        self.assertEqual(
            self.service.get(self.actor, job["id"])["error_code"],
            "system_cleanup_failed",
        )
        self.assertIsNotNone(ProfileRepository(self.db).get(profile))
        self.assertEqual(self.service.abort(self.actor, job["id"])["status"], "aborted")
        self.host.clear_local.assert_not_called()

    def test_remove_waits_for_ack_and_then_launches_once(self):
        job = self.queue("remove")
        self.advance(job, "awaiting_shutdown")
        self.assertFalse(self.service.run_one())
        self.host.launch_uninstall.assert_not_called()
        self.assertIsNotNone(self.identities.find_telegram_account(101))
        self.service.acknowledge_shutdown(self.actor, job["id"])
        self.assertTrue(self.service.run_one())
        self.host.launch_uninstall.assert_called_once()
        self.assertFalse(self.service.run_one())
        self.assertIsNone(self.identities.find_telegram_account(101))

    def test_full_removal_skips_backup_even_when_backup_storage_fails(self):
        self.service.backups = Mock()
        self.service.backups.create_snapshot.side_effect = RuntimeError("unavailable")
        job = self.queue("remove")
        self.assertEqual(job["phase"], "nodes")
        self.advance(job, "awaiting_shutdown")
        self.service.backups.create_snapshot.assert_not_called()
        self.assertIsNone(self.service.get(self.actor, job["id"])["backup_id"])

    def test_previously_queued_remove_also_skips_backup(self):
        self.service.backups = Mock()
        job = self.queue("remove")
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_system_cleanup_jobs SET phase='backup' WHERE id=?", (job["id"],))
        self.advance(job, "awaiting_shutdown")
        self.service.backups.create_snapshot.assert_not_called()

    def test_uncertain_shutdown_never_replays(self):
        job = self.queue("remove")
        self.advance(job, "awaiting_shutdown")
        self.host.launch_uninstall.side_effect = TimeoutError()
        self.service.acknowledge_shutdown(self.actor, job["id"])
        self.service.run_one()
        self.assertFalse(self.service.run_one())
        self.host.launch_uninstall.assert_called_once()
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT status,phase FROM backend_system_cleanup_jobs"
            ).fetchone()
            self.assertEqual(tuple(row), ("blocked", "uninstall_launch"))

    def test_abort_discards_prepared_script_without_shutdown(self):
        job = self.queue("remove")
        self.advance(job, "awaiting_shutdown")
        self.service.abort(self.actor, job["id"])
        self.host.discard_uninstall.assert_called_once_with(
            self.host.prepare_uninstall.return_value
        )
        self.host.launch_uninstall.assert_not_called()

    def test_cancel_queued_removal_stops_before_host_preparation(self):
        job = self.queue("remove")
        pending = self.service.abort(self.actor, job["id"])
        self.assertTrue(pending["cancel_requested"])
        self.assertEqual(pending["status"], "queued")
        self.service.run_one()
        self.assertEqual(self.service.get(self.actor, job["id"])["status"], "aborted")
        self.host.prepare_uninstall.assert_not_called()
        self.host.launch_uninstall.assert_not_called()
        with self.db.transaction() as conn:
            admit(conn)

    def test_cancel_between_nodes_does_not_start_the_next_node(self):
        with self.db.transaction() as conn:
            for key in ("one", "two"):
                conn.execute("INSERT INTO backend_nodes(key,title,region,protocols_json) VALUES (?,?,'EU','[]')", (key, key))
                conn.execute("INSERT INTO backend_node_connections(node_key,transport,ssh_target) VALUES (?,'ssh',?)", (key, f"root@{key}.example"))
        self.service.removals = Mock()
        job = self.queue("remove", nodes=True)
        def request(*args):
            self.service.abort(self.actor, job["id"])
            return {"status": "running"}
        self.service.removals.request.side_effect = request
        self.service.run_one()
        self.assertEqual(self.service.removals.request.call_count, 1)
        self.service.run_one()
        self.assertEqual(self.service.get(self.actor, job["id"])["status"], "aborted")
        self.assertEqual(self.service.removals.request.call_count, 1)
        self.host.prepare_uninstall.assert_not_called()

    def test_cancel_during_preparation_blocks_shutdown_and_discards_script(self):
        job = self.queue("remove")
        self.service.run_one()  # nodes -> uninstall_prepare
        def prepare(*args):
            self.service.abort(self.actor, job["id"])
            return {"unit": "fake-unit", "script": "/not-executed"}
        self.host.prepare_uninstall.side_effect = prepare
        self.service.run_one()
        self.assertTrue(self.service.get(self.actor, job["id"])["cancel_requested"])
        with self.assertRaises(AccessDenied):
            self.service.acknowledge_shutdown(self.actor, job["id"])
        self.service.run_one()
        self.assertEqual(self.service.get(self.actor, job["id"])["status"], "aborted")
        self.host.discard_uninstall.assert_called_once()
        self.host.launch_uninstall.assert_not_called()

    def test_cancel_is_rejected_after_shutdown_confirmation(self):
        job = self.queue("remove")
        self.advance(job, "awaiting_shutdown")
        self.service.acknowledge_shutdown(self.actor, job["id"])
        self.assertFalse(self.service.get(self.actor, job["id"])["can_abort"])
        with self.assertRaises(AccessDenied) as error:
            self.service.abort(self.actor, job["id"])
        self.assertEqual(error.exception.code, "cleanup_abort_unsafe")

    def test_cancel_script_discard_failure_does_not_replay_or_restart_removal(self):
        job = self.queue("remove")
        self.service.run_one()
        self.host.prepare_uninstall.side_effect = lambda *args: (
            self.service.abort(self.actor, job["id"]) and {"unit": "fake-unit", "script": "/not-executed"})
        self.service.run_one()
        self.host.discard_uninstall.side_effect = RuntimeError("discard failed")
        self.service.run_one()
        self.assertEqual(self.service.get(self.actor, job["id"])["status"], "aborted")
        self.assertFalse(self.service.run_one())
        self.host.launch_uninstall.assert_not_called()

    def test_workstation_shutdown_ack_accepts_an_empty_http_body(self):
        from backend.http_api import create_app
        from fastapi.testclient import TestClient
        job = self.queue("remove")
        self.advance(job, "awaiting_shutdown")
        with TestClient(create_app(self.db, cleanup_host=self.host)) as client:
            headers = {**self.headers, "X-Node-Plane-Telegram-User-ID": "101"}
            response = client.post(f"/api/v1/system/cleanup/jobs/{job['id']}/shutdown-ack", headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["shutdown_ack"])

    def test_http_starts_removal_with_a_local_node(self):
        from backend.http_api import create_app
        from fastapi.testclient import TestClient
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_nodes(key,title,region,protocols_json) VALUES ('local','Local','EU','[]')")
            conn.execute("INSERT INTO backend_node_connections(node_key,transport) VALUES ('local','local')")
        with TestClient(create_app(self.db, cleanup_host=self.host)) as client:
            headers = {**self.headers, "X-Node-Plane-Telegram-User-ID": "101", "Idempotency-Key": str(uuid4())}
            plan = client.post("/api/v1/system/cleanup/plans", headers=headers,
                               json={"action": "remove", "cleanup_nodes": True})
            self.assertEqual(plan.status_code, 200, plan.text)
            queued = client.post("/api/v1/system/cleanup/jobs", headers=headers,
                                 json={"plan_id": plan.json()["id"], "confirmation_phrase": "REMOVE NODE PLANE"})
            self.assertEqual(queued.status_code, 202, queued.text)
            self.assertEqual(queued.json()["items"][0]["node_key"], "local")

    def test_remote_failure_keeps_controller_state_and_retry_is_explicit(self):
        profile = ProfileRepository(self.db).create_profile(
            runtime_name="alice", display_name="Alice"
        )
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO backend_nodes(key,title,region,protocols_json) VALUES ('lv1','Latvia','EU','[]')"
            )
            conn.execute(
                "INSERT INTO backend_node_connections(node_key,transport,ssh_target) VALUES ('lv1','ssh','root@lv1.example')"
            )
        removals = Mock()
        removals.request.return_value = {
            "status": "blocked",
            "error_code": "agent_unreachable",
        }
        self.service.removals = removals
        job = self.queue(nodes=True)
        self.advance(job, "blocked")
        self.assertIsNotNone(ProfileRepository(self.db).get(profile))
        self.host.clear_local.assert_not_called()
        before = removals.request.call_count
        self.assertFalse(self.service.run_one())
        self.assertEqual(removals.request.call_count, before)
        removals.request.return_value = {"status": "removed"}
        self.service.retry(self.actor, job["id"])
        self.advance(job, "succeeded")
        self.assertTrue(
            any(call.kwargs.get("retry") for call in removals.request.call_args_list)
        )

    def test_http_requires_admin_strict_scope_and_matching_confirmation(self):
        from backend.http_api import create_app
        from fastapi.testclient import TestClient

        with TestClient(create_app(self.db, cleanup_host=self.host)) as client:
            headers = {**self.headers, "X-Node-Plane-Telegram-User-ID": "101"}
            path = "/api/v1/system/cleanup/plans"
            denied_guest = client.post(
                path,
                headers={**headers, "X-Node-Plane-Telegram-User-ID": "102"},
                json={"action": "reset", "cleanup_nodes": False},
            )
            self.assertEqual(denied_guest.status_code, 401)
            resp = fixture.BackendHTTPTests.provision(self, 102)
            self.assertIn(resp.status_code, (200, 201))
            self.assertEqual(
                client.post(
                    path,
                    headers={**headers, "X-Node-Plane-Telegram-User-ID": "102"},
                    json={"action": "reset", "cleanup_nodes": False},
                ).status_code,
                403,
            )
            for body in (
                {"action": "reset", "cleanup_nodes": "false"},
                {"action": "reset", "cleanup_nodes": False, "base_dir": "/"},
            ):
                self.assertEqual(
                    client.post(path, headers=headers, json=body).status_code, 422
                )
            plan = client.post(
                path, headers=headers, json={"action": "reset", "cleanup_nodes": False}
            ).json()
            headers["Idempotency-Key"] = str(uuid4())
            endpoint = "/api/v1/system/cleanup/jobs"
            self.assertEqual(
                client.post(
                    endpoint,
                    headers=headers,
                    json={"plan_id": plan["id"], "confirmation_phrase": "yes"},
                ).status_code,
                422,
            )
            queued = client.post(
                endpoint,
                headers=headers,
                json={
                    "plan_id": plan["id"],
                    "confirmation_phrase": plan["confirmation_phrase"],
                },
            )
            self.assertEqual(queued.status_code, 202, queued.text)
            self.assertEqual(
                client.get(
                    endpoint + "/" + queued.json()["id"], headers=headers
                ).status_code,
                200,
            )
            self.assertEqual(
                client.post(
                    "/api/v1/integrations/telegram/identities/resolve",
                    headers=headers,
                    json={"telegram_user_id": 103},
                ).status_code,
                409,
            )

    def test_cleanup_visible_and_initiating_account_retry_abort_with_rotated_principal_works_while_other_account_denied(self):
        from backend.recovery import overview
        from backend.authorization import PrincipalKind, ADMIN_PERMISSIONS
        job = self.queue(action="reset", nodes=False)
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_system_cleanup_jobs SET status='blocked', phase='backup' WHERE id=?", (job["id"],))

        # Initiating account sees actions
        items = overview(self.db, self.actor)["items"]
        cleanup_items = [i for i in items if i["kind"] == "cleanup"]
        self.assertEqual(len(cleanup_items), 1)
        self.assertEqual(cleanup_items[0]["id"], job["id"])
        self.assertIn("retry", cleanup_items[0]["actions"])
        self.assertIn("abort", cleanup_items[0]["actions"])

        # Another account is denied
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_accounts(id,role,status) VALUES ('admin-other','admin','approved')")
        _, other_token = self.credentials.issue(PrincipalKind.ACCOUNT, ADMIN_PERMISSIONS, account_id="admin-other")
        other_account = type(self.admin)("admin-other", "admin", "approved")
        other_actor = Actor(self.credentials.authenticate("Bearer " + other_token), other_account)

        items_other = overview(self.db, other_actor)["items"]
        cleanup_other = [i for i in items_other if i["kind"] == "cleanup"][0]
        self.assertEqual(cleanup_other["actions"], [])

        with self.assertRaises(AccessDenied):
            self.service.retry(other_actor, job["id"])
        with self.assertRaises(AccessDenied):
            self.service.abort(other_actor, job["id"])

        # Same account with rotated principal (new credential) works
        _, new_token = self.credentials.issue(PrincipalKind.ACCOUNT, ADMIN_PERMISSIONS, account_id=self.admin.id)
        rotated_actor = Actor(self.credentials.authenticate("Bearer " + new_token), self.admin)

        retried = self.service.retry(rotated_actor, job["id"])
        self.assertEqual(retried["status"], "running")

        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_system_cleanup_jobs SET status='blocked' WHERE id=?", (job["id"],))

        aborted = self.service.abort(rotated_actor, job["id"])
        self.assertEqual(aborted["status"], "aborted")
