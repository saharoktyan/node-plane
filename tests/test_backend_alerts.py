"""Monitor transitions, unknown measurements and transport authorization."""

import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
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
        "cpu_used_percent": 20, "cpu_sample_window_seconds": 60, "cpu_sample_age_seconds": 0,
    },
}


class RpcFailure(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNAVAILABLE


class AlertTests(unittest.TestCase):
    def test_agent_only_host_alert_is_visible_and_delivered_without_vpn_enablement(self):
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_nodes SET protocols_json='[]',applied_revision=0,enabled=0 WHERE key='test'")
            conn.execute("INSERT INTO backend_agent_rollouts(id,node_key,actor_account_id,command_key,intent_json,status) VALUES (?,'test',?,?,'{}','succeeded')",
                         (str(uuid4()), self.admin.id, str(uuid4())))
        self.driver.inspect_node_services.return_value = {**HEALTHY, 'host_metrics': {**HEALTHY['host_metrics'], 'disk_free_percent': 3}}
        self.scan()
        active = self.service.overview(self.actor)['active']
        self.assertEqual([item['kind'] for item in active], ['disk_low'])
        delivery = self.service.claim(self.transport, str(uuid4()))
        self.assertIsNotNone(delivery)
        self.assertEqual(delivery['event']['kind'], 'disk_low')
        self.driver.host_metrics = Mock(return_value={'node_key': 'test',
            'host_metrics': {**HEALTHY['host_metrics'], 'cpu_used_percent': 99}})
        self.assertTrue(self.service.scheduled(cpu_only=True))
        self.assertEqual({item['kind'] for item in self.service.overview(self.actor)['active']}, {'disk_low', 'cpu_high'})
        self.assertEqual(self.driver.inspect_node_services.call_count, 1)

    def test_agent_only_connection_outage_is_monitored_but_agent_upgrade_is_not_an_outage(self):
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_nodes SET protocols_json='[]',applied_revision=0,enabled=0 WHERE key='test'")
            conn.execute("INSERT INTO backend_agent_rollouts(id,node_key,actor_account_id,command_key,intent_json,status) VALUES (?,'test',?,?,'{}','succeeded')",
                         (str(uuid4()), self.admin.id, str(uuid4())))
            upgrade = str(uuid4())
            conn.execute("INSERT INTO backend_agent_rollouts(id,node_key,actor_account_id,command_key,intent_json,status) VALUES (?,'test',?,?,'{}','running')",
                         (upgrade, self.admin.id, str(uuid4())))
        self.driver.inspect_node_services.side_effect = RpcFailure()
        self.driver.host_metrics.side_effect = RpcFailure()
        self.scan()
        self.driver.inspect_node_services.assert_not_called()
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_agent_rollouts SET status='succeeded' WHERE id=?", (upgrade,))
        self.scan()
        self.scan()
        self.assertEqual([item['kind'] for item in self.service.overview(self.actor)['active']], ['node_unreachable'])

    def setUp(self):
        fixture.BackendHTTPTests.setUp(self)
        # Persistent settings modules initialize their database at import time.
        with patch('db.get_db', return_value=self.db):
            import app.services.app_settings

        self.actor = Actor(
            Principal("monitor-test", PrincipalKind.SERVICE, ADMIN_PERMISSIONS),
            self.admin,
        )
        self.transport = self.credentials.authenticate("Bearer " + self.token)
        self.driver = SimpleNamespace(
            binary_info=Mock(return_value={"version": "test"}),
            inspect_node_services=Mock(return_value=dict(HEALTHY)),
            host_metrics=Mock(return_value=dict(HEALTHY)),
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
        self.scan_time = getattr(self, 'scan_time', datetime(2026, 1, 1, tzinfo=timezone.utc)) + timedelta(seconds=30)
        with patch('backend.alerts.datetime') as clock:
            clock.fromisoformat = datetime.fromisoformat
            clock.now.return_value = self.scan_time
            return self.service.scheduled()

    def events(self):
        return self.db.connection.execute(
            "SELECT kind,resolved FROM backend_alert_events ORDER BY created_at"
        ).fetchall()

    def test_alert_rate_limit_pauses_announcements_on_the_same_adapter(self):
        from backend.announcements import AnnouncementService
        self.driver.inspect_node_services.return_value = {**HEALTHY, 'xray_running': False}
        self.scan()
        key = str(uuid4())
        delivery = self.service.claim(self.transport, key)
        self.assertIsNotNone(delivery)
        self.service.acknowledge(self.transport, delivery['id'], key, 'retry', 60)
        self.assertIsNone(self.service.claim(self.transport, str(uuid4())))
        resp = fixture.BackendHTTPTests.provision(self, 102)
        self.assertIn(resp.status_code, (200, 201))
        announcements = AnnouncementService(self.db)
        queued = announcements.queue(self.actor, 'Hello', str(uuid4()))
        self.assertEqual(queued['total'], 1)
        self.assertIsNone(announcements.claim(self.transport, str(uuid4())))

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

    def test_dismiss_is_per_admin_preserves_fault_and_new_occurrence_needs_attention(self):
        self.driver.inspect_node_services.return_value = {**HEALTHY, 'xray_running':False}
        self.scan()
        event = self.service.overview(self.actor)['active'][0]['event_id']
        headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID':'101'}
        response = self.client.post('/api/v1/system/alerts/' + event + '/dismiss', headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        value = self.service.overview(self.actor)
        self.assertEqual(value['active_count'], 1)
        self.assertEqual(value['unacknowledged_count'], 0)
        self.assertTrue(value['active'][0]['dismissed'])
        from backend.admin_overview import AdminOverviewService
        self.assertEqual(AdminOverviewService(self.db).get(self.actor)['unacknowledged_alerts'], 0)
        other = Actor(self.actor.principal, SimpleNamespace(id='another-admin', role='admin', status='approved'))
        self.assertEqual(self.service.overview(other)['unacknowledged_count'], 1)
        self.driver.inspect_node_services.return_value = dict(HEALTHY)
        self.scan()
        self.scan()
        self.driver.inspect_node_services.return_value = {**HEALTHY, 'xray_running':False}
        self.scan()
        self.assertEqual(self.service.overview(self.actor)['unacknowledged_count'], 1)
        with self.assertRaises(AccessDenied):
            self.service.dismiss(self.actor, event)

    def test_attention_reads_known_update_status_without_probing_host(self):
        headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID':'101'}
        with patch('db.get_db', return_value=self.db), \
             patch('app.services.updates.get_updates_overview', return_value={'update_available':True}) as read:
            response = self.client.get('/api/v1/system/attention', headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['update_available'])
        read.assert_called_once_with(refresh_run=False)

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
        self.driver.host_metrics.side_effect = RpcFailure()
        self.scan()
        self.scan()
        self.assertEqual(
            {r["kind"] for r in self.service.overview(self.actor)["active"]},
            {"disk_low", "node_unreachable"},
        )
        self.driver.inspect_node_services.side_effect = None
        self.driver.host_metrics.side_effect = None
        self.driver.inspect_node_services.return_value = dict(HEALTHY)
        self.scan()
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

    def test_vpn_pending_or_disabled_keeps_host_checks_but_uninstalled_and_maintenance_are_excluded(self):
        self.driver.inspect_node_services.return_value.update(awg_running=False, xray_running=False)
        for sql in (
            "UPDATE backend_nodes SET applied_revision=0",
            "UPDATE backend_nodes SET applied_revision=1,desired_revision=2",
            "UPDATE backend_nodes SET applied_revision=1,desired_revision=1,enabled=0",
        ):
            self.db.connection.execute(sql)
            self.db.connection.commit()
            self.scan()
        self.assertEqual(self.driver.inspect_node_services.call_count, 2)
        self.assertEqual(self.service.overview(self.actor)['active_count'], 0)
        self.db.connection.execute("UPDATE backend_nodes SET enabled=1")
        self.db.connection.execute(
            "INSERT INTO backend_node_drains VALUES ('test',?,'[]','now')",
            (self.admin.id,),
        )
        self.db.connection.commit()
        self.scan()
        self.assertEqual(self.driver.inspect_node_services.call_count, 2)

    def test_alert_interval_choices_and_legacy_fifteen_minute_policy(self):
        for minutes in (3,5,10):
            result = self.service.preferences(self.actor,{'interval_minutes':minutes})
            self.assertEqual(result['interval_minutes'],minutes)
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_system_settings SET value=? WHERE key='alert_policy'", (json.dumps({'enabled':True,'notify_resolved':True,'interval_minutes':15}),))
            self.assertEqual(self.service._policy(conn)['interval_minutes'],10)

    def test_interval_choices_are_accepted_by_the_http_schema(self):
        headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID':'101'}
        for minutes in (3,5,10):
            response = self.client.patch('/api/v1/system/alerts/preferences',headers=headers,json={'interval_minutes':minutes})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['interval_minutes'],minutes)
        self.assertEqual(self.client.patch('/api/v1/system/alerts/preferences',headers=headers,json={'interval_minutes':15}).status_code,422)

    def test_cpu_checks_run_every_thirty_seconds_independently_of_service_interval(self):
        from datetime import datetime, timedelta, timezone
        start = datetime.now(timezone.utc)
        self.service.preferences(self.actor, {'interval_minutes':10})
        self.driver.host_metrics = Mock(return_value={'host_metrics':{**HEALTHY['host_metrics'],'cpu_used_percent':96}})
        with patch('backend.alerts.datetime') as clock:
            clock.fromisoformat = datetime.fromisoformat
            clock.now.return_value = start
            self.assertTrue(self.service.scheduled())
            self.assertTrue(self.service.scheduled(cpu_only=True))
            self.assertEqual(self.driver.inspect_node_services.call_count,1)
            clock.now.return_value = start + timedelta(seconds=29)
            self.assertFalse(self.service.scheduled(cpu_only=True))
            clock.now.return_value = start + timedelta(seconds=30)
            self.assertTrue(self.service.scheduled(cpu_only=True))
            self.assertTrue(self.service.scheduled())
        self.assertEqual([(e['kind'],e['resolved']) for e in self.events()],[('cpu_high',0)])

    def test_cpu_severity_changes_unknown_samples_and_recovery_use_current_measurements(self):
        from datetime import datetime, timedelta, timezone
        start = datetime.now(timezone.utc)
        self.driver.host_metrics = Mock()
        samples = [86,96,94,89,82,None,79,79]
        with patch('backend.alerts.datetime') as clock:
            clock.fromisoformat = datetime.fromisoformat
            for index,value in enumerate(samples):
                clock.now.return_value = start + timedelta(seconds=index*30)
                self.driver.host_metrics.return_value = {'host_metrics':{**HEALTHY['host_metrics'], 'cpu_used_percent':value}}
                self.assertTrue(self.service.scheduled(cpu_only=True))
                if value is None:
                    self.assertEqual(self.service.overview(self.actor)['active_count'],1)
        events = self.db.connection.execute("SELECT resolved,payload_json FROM backend_alert_events WHERE kind='cpu_high' ORDER BY created_at").fetchall()
        self.assertEqual([e['resolved'] for e in events],[0,0,1])
        payloads = [json.loads(e['payload_json']) for e in events]
        self.assertEqual([p.get('severity') for p in payloads[:2]],['warning','critical'])
        self.assertEqual(payloads[-1]['value'],79)
        self.assertEqual(self.service.overview(self.actor)['active_count'],0)

    def test_cpu_short_stale_or_malformed_windows_are_unknown(self):
        for key,value in [('cpu_sample_window_seconds',10),('cpu_sample_age_seconds',16),('cpu_used_percent',True),('cpu_used_percent',float('nan')),('cpu_used_percent',101)]:
            metrics = {**HEALTHY['host_metrics'],key:value}
            failures,known = self.service.classify({'host_metrics':metrics},[])
            self.assertNotIn('cpu_high',known)
            self.assertNotIn('cpu_high',failures)

    def test_resource_recovery_notifies_with_new_value_instead_of_old_alarm_value(self):
        self.driver.inspect_node_services.return_value = {**HEALTHY,'host_metrics':{**HEALTHY['host_metrics'],'disk_free_percent':3}}
        self.scan()
        self.driver.inspect_node_services.return_value = {**HEALTHY,'host_metrics':{**HEALTHY['host_metrics'],'disk_free_percent':18}}
        self.scan()
        self.scan()
        row = self.db.connection.execute("SELECT payload_json FROM backend_alert_events WHERE kind='disk_low' AND resolved=1").fetchone()
        self.assertEqual(json.loads(row['payload_json'])['value'],18)

    def test_preferences_and_interval_are_enforced(self):
        self.assertTrue(self.service.scheduled())
        self.assertFalse(self.service.scheduled())
        for changes in (
            {"interval_minutes": True},
            {"interval_minutes": 15},
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

    def test_events_target_every_approved_admin_and_skip_other_accounts(self):
        resp_second = fixture.BackendHTTPTests.provision(self, 102)
        self.assertIn(resp_second.status_code, (200, 201))
        second = resp_second.json()['account']
        resp_third = fixture.BackendHTTPTests.provision(self, 103)
        self.assertIn(resp_third.status_code, (200, 201))
        third = resp_third.json()['account']
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_accounts SET role='admin',status='approved' WHERE id=?", (second['id'],))
            conn.execute("UPDATE backend_accounts SET role='admin',status='disabled' WHERE id=?", (third['id'],))
        self.driver.inspect_node_services.return_value = {**HEALTHY, 'awg_running':False}
        self.scan()
        recipients = set()
        while delivery := self.service.claim(self.transport, str(uuid4())):
            recipients.add(delivery['telegram_user_id'])
        self.assertEqual(recipients, {101,102})

    def test_update_notice_is_deduplicated_and_independent_of_monitoring(self):
        from backend.updates import UpdateService
        self.service.preferences(self.actor, {"enabled": False})
        updater = UpdateService(self.db)
        result = dict(remote_version='9.0.0', remote_label='9.0.0',
                      upstream_ref='v9.0.0', changelog='New features')
        updater._notify_update(result, 'dev', 'tag')
        updater._notify_update(result, 'dev', 'tag')
        self.assertEqual(len(self.events()), 1)
        self.service.preferences(self.actor, {"enabled": False})
        self.assertEqual(self.service.overview(self.actor)['delivery_counts']['queued'], 0)
        with patch('app.services.app_settings.is_updates_auto_check_enabled', return_value=True), \
             patch('app.services.app_settings.get_updates_branch', return_value='dev'), \
             patch('app.services.app_settings.get_updates_dev_track', return_value='tag'):
            delivery = self.service.claim(self.transport, str(uuid4()))
        self.assertEqual(delivery['telegram_user_id'], 101)
        self.assertEqual(delivery['event']['kind'], 'update_available')
        self.assertEqual(delivery['event']['payload']['changelog'], 'New features')

    def test_queued_update_notice_rechecks_channel_policy_and_admin(self):
        from backend.updates import UpdateService
        for change in ('off', 'branch', 'role'):
            with self.subTest(change=change):
                UpdateService(self.db)._notify_update(dict(remote_label=change,
                    upstream_ref=change, changelog=''), 'dev', 'tag')
                if change == 'role':
                    self.db.connection.execute("UPDATE backend_accounts SET role='member' WHERE id=?", (self.admin.id,))
                    self.db.connection.commit()
                with patch('app.services.app_settings.is_updates_auto_check_enabled', return_value=change != 'off'), \
                     patch('app.services.app_settings.get_updates_branch', return_value='main' if change == 'branch' else 'dev'), \
                     patch('app.services.app_settings.get_updates_dev_track', return_value='tag'):
                    self.assertIsNone(self.service.claim(self.transport, str(uuid4())))

    def test_auto_check_respects_frequency_and_notifies_only_available_releases(self):
        from backend.updates import UpdateService
        from datetime import datetime, timezone
        updater = Mock()
        updater.check_for_updates.return_value = dict(status='available', remote_label='9.0.0',
            upstream_ref='v9.0.0', changelog='')
        service = UpdateService(self.db, updater=updater)
        with patch('app.services.app_settings.is_updates_auto_check_enabled', return_value=True), \
             patch('app.services.app_settings.get_updates_branch', return_value='dev'), \
             patch('app.services.app_settings.get_updates_dev_track', return_value='tag'), \
             patch('app.services.app_settings.get_updates_check_interval_minutes', return_value=15), \
             patch('app.services.app_settings.get_update_state') as state:
            state.return_value = {'last_checked_at': datetime.now(timezone.utc).isoformat()}
            service.auto_check()
            updater.check_for_updates.assert_not_called()
            state.return_value = {'last_checked_at': '2000-01-01T00:00:00Z'}
            service.auto_check()
            service.auto_check()
            self.assertEqual(len(self.events()), 1)
            updater.check_for_updates.return_value = {'status':'up_to_date'}
            service.auto_check()
            self.assertEqual(len(self.events()), 1)

    def test_member_http_access_denied(self):
        denied_guest = self.client.post('/api/v1/system/alerts/' + str(uuid4()) + '/dismiss',
            headers={**self.headers, "X-Node-Plane-Telegram-User-ID": "102"})
        self.assertEqual(denied_guest.status_code, 401)
        resp = fixture.BackendHTTPTests.provision(self, 102)
        self.assertIn(resp.status_code, (200, 201))
        headers = {**self.headers, "X-Node-Plane-Telegram-User-ID": "102"}
        self.assertEqual(self.client.post('/api/v1/system/alerts/' + str(uuid4()) + '/dismiss',
            headers=headers).status_code, 403)
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
