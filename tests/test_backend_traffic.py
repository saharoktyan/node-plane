"""Real backend admission/consent with native-counter transport test doubles."""

import base64
import json
import unittest
from unittest.mock import patch

from backend.authorization import (
    ADMIN_PERMISSIONS,
    SELF_PERMISSIONS,
    Actor,
    Principal,
    PrincipalKind,
)
from backend.operations import OperationRepository
from backend.profiles import ProfileRepository
from backend.system_settings import SystemSettingsService
from backend.traffic import TrafficService

from tests import test_backend_config_issuance as config_fixture
from tests import test_backend_http as fixture


class Driver:
    def __init__(self):
        self.calls = []
        self.up, self.down, self.epoch = 1000, 2000, "a" * 64
        self.error = False
        self.hook = None

    def traffic_snapshot(self, intent):
        self.calls.append(intent)
        if self.hook:
            self.hook()
        if self.error:
            raise ConnectionError("raw private runtime diagnostic")
        return {
            **intent,
            "epoch": self.epoch,
            "uplink_bytes": self.up,
            "downlink_bytes": self.down,
        }


class TrafficTests(unittest.TestCase):
    def setUp(self):
        fixture.BackendHTTPTests.setUp(self)
        self.profile_id = config_fixture.BackendConfigIssuanceTests.setup_ready_profile(
            self
        )
        self.actor = Actor(
            Principal(
                "test", PrincipalKind.SERVICE, ADMIN_PERMISSIONS | SELF_PERMISSIONS
            ),
            self.admin,
        )
        self.settings = SystemSettingsService(self.db)
        self.settings.update_traffic_policy(self.actor, True)
        self.settings.update_traffic_consent(self.actor, True)
        self.driver = Driver()
        self.service = TrafficService(self.db, self.driver)

    def collect(self):
        self.db.connection.execute(
            "DELETE FROM backend_system_settings WHERE key='traffic_last_scan'"
        )
        self.db.connection.commit()
        return self.service.scheduled()

    def row(self):
        return self.db.connection.execute(
            "SELECT * FROM backend_traffic_usage"
        ).fetchone()

    def test_first_sample_excludes_previous_traffic_and_repeated_counters_do_not_double_count(
        self,
    ):
        self.assertTrue(self.collect())
        self.assertEqual(self.row()["uplink_bytes"], 0)
        self.assertFalse(self.service.scheduled())
        self.driver.up, self.driver.down = 1100, 2250
        self.collect()
        self.assertEqual(
            (self.row()["uplink_bytes"], self.row()["downlink_bytes"]), (100, 250)
        )
        self.collect()
        self.assertEqual(self.row()["uplink_bytes"], 100)
        headers = {**self.headers, "X-Node-Plane-Telegram-User-ID": "101"}
        summary = self.client.get(
            f"/api/v1/me/profiles/{self.profile_id}/summary", headers=headers
        )
        self.assertEqual(summary.status_code, 200, summary.text)
        self.assertEqual(summary.json()["traffic"]["items"][0]["uplink_bytes"], 100)

    def test_restart_reset_in_each_direction_and_new_epoch(self):
        self.collect()
        self.driver.up, self.driver.down = 50, 2200
        self.collect()
        self.assertEqual(
            (self.row()["uplink_bytes"], self.row()["downlink_bytes"]), (50, 200)
        )
        self.driver.epoch = "b" * 64
        self.driver.up, self.driver.down = 80, 90
        self.collect()
        self.assertEqual(
            (self.row()["uplink_bytes"], self.row()["downlink_bytes"]), (130, 290)
        )

    def test_outage_keeps_baseline_and_last_totals_without_raw_error(self):
        self.collect()
        self.driver.error = True
        self.collect()
        self.assertEqual(self.row()["last_uplink"], 1000)
        self.assertEqual(self.row()["status"], "unknown")
        self.assertEqual(
            self.service.summary(self.admin.id, self.profile_id)["status"], "unknown"
        )
        self.driver.error = False
        self.driver.up = 1200
        self.collect()
        self.assertEqual(self.row()["uplink_bytes"], 200)

    def test_first_failed_observation_is_unknown_not_zero_usage(self):
        self.driver.error = True
        self.collect()
        summary = self.service.summary(self.admin.id, self.profile_id)
        self.assertEqual(summary, {"status": "unknown", "items": []})
        self.assertIsNone(self.row()["last_sample_at"])

    def test_opt_out_purges_history_and_an_in_flight_response_cannot_revive_it(self):
        self.collect()
        self.driver.hook = lambda: self.settings.update_traffic_consent(
            self.actor, False
        )
        self.collect()
        self.assertIsNone(self.row())
        self.assertEqual(
            self.service.summary(self.admin.id, self.profile_id)["status"],
            "consent_required",
        )
        self.driver.calls.clear()
        self.driver.hook = None
        self.collect()
        self.assertEqual(self.driver.calls, [])

    def test_off_on_consent_race_is_fenced_even_when_final_value_matches(self):
        def revoke_and_regrant():
            self.settings.update_traffic_consent(self.actor, False)
            self.settings.update_traffic_consent(self.actor, True)

        self.driver.hook = revoke_and_regrant
        self.collect()
        self.assertIsNone(self.row())

    def test_global_pause_keeps_totals_but_does_not_count_paused_traffic(self):
        self.collect()
        self.driver.up = 1100
        self.collect()
        self.settings.update_traffic_policy(self.actor, False)
        self.assertIsNone(self.service.summary(self.admin.id, self.profile_id))
        self.assertFalse(self.collect())
        self.settings.update_traffic_policy(self.actor, True)
        self.driver.up = 5000
        self.collect()
        self.assertEqual(self.row()["uplink_bytes"], 100)
        self.driver.up = 5050
        self.collect()
        self.assertEqual(self.row()["uplink_bytes"], 150)

    def test_repeated_enable_does_not_reset_baseline(self):
        self.collect()
        self.settings.update_traffic_policy(self.actor, True)
        self.settings.update_traffic_consent(self.actor, True)
        self.driver.up = 1100
        self.collect()
        self.assertEqual(self.row()["uplink_bytes"], 100)

    def test_frozen_unsynced_pending_node_and_revoked_grants_are_not_queried(self):
        for sql, undo in (
            (
                "UPDATE backend_profiles SET frozen=1",
                "UPDATE backend_profiles SET frozen=0",
            ),
            (
                "UPDATE backend_profiles SET desired_revision=2",
                "UPDATE backend_profiles SET desired_revision=1",
            ),
            (
                "UPDATE backend_nodes SET desired_revision=2",
                "UPDATE backend_nodes SET desired_revision=1",
            ),
            (
                "UPDATE backend_operation_tasks SET status='blocked'",
                "UPDATE backend_operation_tasks SET status='succeeded'",
            ),
        ):
            self.db.connection.execute(sql)
            self.db.connection.commit()
            self.collect()
            self.assertEqual(self.driver.calls, [])
            self.db.connection.execute(undo)
            self.db.connection.commit()
        self.db.connection.execute("DELETE FROM backend_grants")
        self.db.connection.commit()
        self.collect()
        self.assertEqual(self.driver.calls, [])

    def test_profile_revision_changes_while_reading_discard_sample(self):
        self.driver.hook = lambda: self.db.connection.execute(
            "UPDATE backend_profiles SET desired_revision=desired_revision+1"
        )
        self.collect()
        self.assertIsNone(self.row())

    def test_unconsenting_and_orphan_profiles_have_no_reads(self):
        self.settings.update_traffic_consent(self.actor, False)
        self.collect()
        self.assertEqual(self.driver.calls, [])
        self.settings.update_traffic_consent(self.actor, True)
        self.db.connection.execute("UPDATE backend_profiles SET owner_account_id=NULL")
        self.db.connection.commit()
        self.collect()
        self.assertEqual(self.driver.calls, [])

    def test_identity_mismatch_negative_and_boolean_counters_are_unknown(self):
        for field, value in (
            ("identity", "wrong"),
            ("uplink_bytes", -1),
            ("downlink_bytes", True),
            ("epoch", "wrong"),
        ):
            with patch.object(
                self.driver,
                "traffic_snapshot",
                return_value={
                    "node_key": "n1",
                    "protocol": "xray",
                    "runtime_name": "alice",
                    "identity": self.db.connection.execute(
                        "SELECT xray_uuid FROM backend_profile_identities"
                    ).fetchone()[0],
                    "epoch": "a" * 64,
                    "uplink_bytes": 1,
                    "downlink_bytes": 2,
                    field: value,
                },
            ):
                self.collect()
                self.assertEqual(self.row()["status"], "unknown")
                self.assertIsNone(self.row()["epoch"])

    def test_ownership_change_purges_history_and_other_account_cannot_read_it(self):
        self.collect()
        member = fixture.BackendHTTPTests.register(self, 102).json()
        self.db.connection.execute(
            "UPDATE backend_accounts SET status='approved' WHERE id=?", (member["id"],)
        )
        self.db.connection.execute(
            "UPDATE backend_profiles SET owner_account_id=?", (member["id"],)
        )
        self.db.connection.commit()
        self.collect()
        self.assertIsNone(self.row())
        headers = {**self.headers, "X-Node-Plane-Telegram-User-ID": "101"}
        self.assertEqual(
            self.client.get(
                f"/api/v1/me/profiles/{self.profile_id}/summary", headers=headers
            ).status_code,
            404,
        )

    def test_awg_expected_public_key_is_from_latest_confirmed_client_config(self):
        pub = base64.b64encode(b"k" * 32).decode()
        self.db.connection.execute(
            "UPDATE backend_nodes SET protocols_json='[\"awg\"]'"
        )
        self.db.connection.execute("UPDATE backend_grants SET protocol='awg'")
        self.db.connection.execute(
            "UPDATE backend_operation_tasks SET protocol='awg',result_json=?",
            (
                json.dumps(
                    {
                        "wg_conf": "[Interface]\nPrivateKey = private\nPublicKey = "
                        + pub
                        + "\n[Peer]\nPublicKey = other"
                    }
                ),
            ),
        )
        self.db.connection.commit()
        self.collect()
        self.assertEqual(self.driver.calls[0]["identity"], pub)
        self.assertEqual(self.row()["protocol"], "awg")

    def test_bounded_batches_rotate_without_pausing_unsampled_baselines(self):
        other = ProfileRepository(self.db).create_profile(
            runtime_name="other", display_name="Other", owner_account_id=self.admin.id
        )
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_grants VALUES (?,'n1','xray')", (other,))
            OperationRepository.record(conn, self.actor, other, set())
            conn.execute("UPDATE backend_operation_tasks SET status='succeeded'")
        with patch("backend.traffic.BATCH_SIZE", 1):
            self.collect()
            self.collect()
        self.assertEqual(
            {call["runtime_name"] for call in self.driver.calls}, {"alice", "other"}
        )
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM backend_traffic_usage WHERE epoch IS NOT NULL"
            ).fetchone()[0],
            2,
        )
