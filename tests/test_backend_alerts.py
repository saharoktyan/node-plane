"""Monitor transitions, unknown measurements and transport authorization."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import grpc
from backend.alerts import AlertService
from backend.authorization import (
    ADMIN_PERMISSIONS,
    AccessDenied,
    Actor,
    Principal,
    PrincipalKind,
)

from tests import test_backend_http as fixture

HEALTHY = {
    "inspection_available": True,
    "awg_running": True,
    "xray_running": True,
    "host_metrics": {
        "disk_free_percent": 50,
        "ram_used_percent": 40,
        "load1": 0.1,
        "cpus": 2,
    },
}


class RpcFailure(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNAVAILABLE


class AlertTests(unittest.TestCase):
    def setUp(self):
        fixture.BackendHTTPTests.setUp(self)
        self.actor = Actor(
            Principal("monitor-test", PrincipalKind.SERVICE, ADMIN_PERMISSIONS),
            self.admin,
        )
        self.transport = self.credentials.authenticate("Bearer " + self.token)
        self.driver = SimpleNamespace(
            binary_info=Mock(return_value={"version": "test"}),
            inspect_node_services=Mock(return_value=dict(HEALTHY)),
        )
        self.service = AlertService(self.db, self.driver)
        self.db.connection.execute(
            "INSERT INTO backend_nodes(key,title,region,protocols_json,applied_revision) VALUES ('test','Test','EU','[\"xray\",\"awg\"]',1)"
        )
        self.db.connection.commit()
        self.service.preferences(self.actor, {"enabled": True})

    def scan(self):
        self.db.connection.execute(
            "DELETE FROM backend_system_settings WHERE key='alert_last_scan'"
        )
        self.db.connection.commit()
        return self.service.scheduled()

    def events(self):
        return self.db.connection.execute(
            "SELECT kind,resolved FROM backend_alert_events ORDER BY created_at"
        ).fetchall()

    def test_fault_and_recovery_emit_only_on_transitions(self):
        self.driver.inspect_node_services.return_value = {
            **HEALTHY,
            "xray_running": False,
            "inspection_available": True,
        }
        self.scan()
        self.scan()
        self.assertEqual(
            [(r["kind"], r["resolved"]) for r in self.events()], [("xray_down", 0)]
        )
        self.assertEqual(self.service.overview(self.actor)["active_count"], 1)
        self.driver.inspect_node_services.return_value = dict(HEALTHY)
        self.scan()
        self.scan()
        self.assertEqual(
            [(r["kind"], r["resolved"]) for r in self.events()],
            [("xray_down", 0), ("xray_down", 1)],
        )
        self.assertEqual(self.service.overview(self.actor)["active_count"], 0)

    def test_unknown_resources_do_not_resolve_old_condition(self):
        self.driver.inspect_node_services.return_value = {
            **HEALTHY,
            "host_metrics": {"ram_used_percent": 95},
        }
        self.scan()
        self.driver.inspect_node_services.return_value = {
            "xray_running": True,
            "awg_running": True,
        }
        self.scan()
        value = self.service.overview(self.actor)
        self.assertEqual([r["kind"] for r in value["active"]], ["ram_high"])
        self.assertEqual(value["last_scan"]["status"], "partial")
        self.assertEqual(len(self.events()), 1)

    def test_thresholds_and_invalid_measurements(self):
        faults, known = self.service.classify(
            {
                "xray_running": False,
                "inspection_available": True,
                "host_metrics": {
                    "disk_free_percent": 9,
                    "ram_used_percent": 90,
                    "load1": 4,
                    "cpus": 2,
                },
            },
            ["xray"],
        )
        self.assertEqual(
            set(faults), {"xray_down", "disk_low", "ram_high", "load_high"}
        )
        faults, known = self.service.classify(
            {
                "host_metrics": {
                    "disk_free_percent": True,
                    "ram_used_percent": float("nan"),
                    "load1": float("inf"),
                    "cpus": 1,
                }
            },
            [],
        )
        self.assertEqual(faults, {})
        self.assertEqual(known, {"node_unreachable"})

    def test_agent_outage_preserves_prior_resource_state(self):
        self.driver.inspect_node_services.return_value = {
            **HEALTHY,
            "host_metrics": {"disk_free_percent": 5},
        }
        self.scan()
        self.driver.inspect_node_services.side_effect = RpcFailure()
        self.scan()
        self.assertEqual(
            {r["kind"] for r in self.service.overview(self.actor)["active"]},
            {"disk_low", "node_unreachable"},
        )
        self.driver.inspect_node_services.side_effect = None
        self.driver.inspect_node_services.return_value = dict(HEALTHY)
        self.scan()
        self.assertEqual(self.service.overview(self.actor)["active_count"], 0)

    def test_driver_failure_does_not_fabricate_node_outages(self):
        self.driver.binary_info.side_effect = TimeoutError("private diagnostics")
        self.scan()
        value = self.service.overview(self.actor)
        self.assertEqual(value["active_count"], 0)
        self.assertEqual(value["last_scan"]["status"], "failed")
        self.assertNotIn("private", json.dumps(value))
        self.driver.inspect_node_services.assert_not_called()

    def test_installation_pending_settings_and_maintenance_are_excluded(self):
        for sql in (
            "UPDATE backend_nodes SET applied_revision=0",
            "UPDATE backend_nodes SET applied_revision=1,desired_revision=2",
            "UPDATE backend_nodes SET applied_revision=1,desired_revision=1,enabled=0",
        ):
            self.db.connection.execute(sql)
            self.db.connection.commit()
            self.scan()
        self.db.connection.execute("UPDATE backend_nodes SET enabled=1")
        self.db.connection.execute(
            "INSERT INTO backend_node_drains VALUES ('test',?,'[]','now')",
            (self.admin.id,),
        )
        self.db.connection.commit()
        self.scan()
        self.driver.inspect_node_services.assert_not_called()

    def test_preferences_and_interval_are_enforced(self):
        self.assertTrue(self.service.scheduled())
        self.assertFalse(self.service.scheduled())
        for changes in (
            {"interval_minutes": True},
            {"interval_minutes": 10},
            {"enabled": 1},
            {},
        ):
            with self.assertRaises(AccessDenied):
                self.service.preferences(self.actor, changes)
        self.service.preferences(self.actor, {"enabled": False})
        self.assertFalse(self.scan())

    def test_resolved_notifications_can_be_disabled(self):
        self.service.preferences(self.actor, {"notify_resolved": False})
        self.driver.inspect_node_services.return_value = {
            **HEALTHY,
            "awg_running": False,
        }
        self.scan()
        self.driver.inspect_node_services.return_value = dict(HEALTHY)
        self.scan()
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.service.overview(self.actor)["active_count"], 0)

    def test_claim_ack_and_ambiguous_claim_are_not_replayed(self):
        self.driver.inspect_node_services.return_value = {
            **HEALTHY,
            "awg_running": False,
        }
        self.scan()
        key = str(uuid4())
        delivery = self.service.claim(self.transport, key)
        self.assertEqual(delivery["telegram_user_id"], 101)
        self.assertEqual(self.service.claim(self.transport, key), delivery)
        self.service.acknowledge(self.transport, delivery["id"], key, "sent")
        self.service.acknowledge(self.transport, delivery["id"], key, "sent")
        self.assertIsNone(self.service.claim(self.transport, key))
        self.driver.inspect_node_services.return_value = dict(HEALTHY)
        self.scan()
        self.service.claim(self.transport, str(uuid4()))
        self.db.connection.execute(
            "UPDATE backend_alert_deliveries SET claimed_until='2000-01-01T00:00:00+00:00' WHERE status='claimed'"
        )
        self.db.connection.commit()
        self.assertIsNone(self.service.claim(self.transport, str(uuid4())))

    def test_delivery_rechecks_admin_role_and_policy(self):
        self.driver.inspect_node_services.return_value = {
            **HEALTHY,
            "awg_running": False,
        }
        self.scan()
        self.service.preferences(self.actor, {"enabled": False})
        self.assertIsNone(self.service.claim(self.transport, str(uuid4())))
        self.assertEqual(
            self.service.overview(self.actor)["delivery_counts"]["skipped"], 1
        )
        self.service.preferences(self.actor, {"enabled": True})
        self.driver.inspect_node_services.return_value = dict(HEALTHY)
        self.scan()
        self.db.connection.execute(
            "UPDATE backend_accounts SET role='member' WHERE id=?", (self.admin.id,)
        )
        self.db.connection.commit()
        self.assertIsNone(self.service.claim(self.transport, str(uuid4())))

    def test_old_runtime_fallback_does_not_report_stopped_services(self):
        faults, known = self.service.classify(
            {"xray_running": False, "awg_running": False}, ["xray", "awg"]
        )
        self.assertEqual(faults, {})
        self.assertEqual(known, {"node_unreachable"})

    def test_removed_protocol_and_node_alert_records_are_retired(self):
        self.driver.inspect_node_services.return_value = {
            **HEALTHY,
            "xray_running": False,
        }
        self.scan()
        self.db.connection.execute(
            "UPDATE backend_nodes SET protocols_json=?", (json.dumps(["awg"]),)
        )
        self.db.connection.commit()
        self.scan()
        self.assertEqual(self.service.overview(self.actor)["active_count"], 0)
        self.assertEqual(len(self.events()), 1)
        with self.db.transaction() as conn:
            self.service.retire_node(conn, "test")
        self.assertEqual(len(self.events()), 0)
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM backend_alert_deliveries"
            ).fetchone()[0],
            0,
        )

    def test_member_http_access_denied(self):
        fixture.BackendHTTPTests.register(self, 102)
        headers = {**self.headers, "X-Node-Plane-Telegram-User-ID": "102"}
        self.assertEqual(
            self.client.get("/api/v1/system/alerts", headers=headers).status_code, 403
        )
        self.assertEqual(
            self.client.patch(
                "/api/v1/system/alerts/preferences",
                headers=headers,
                json={"enabled": True},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/v1/integrations/telegram/alerts/claim", headers=headers
            ).status_code,
            422,
        )
