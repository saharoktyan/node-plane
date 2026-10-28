"""Durable notification timing is independent of observation cadence."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from backend.alerts import AlertService
from backend.authorization import AccessDenied
from tests import test_backend_alerts as fixture

HEALTHY = fixture.HEALTHY


class NotificationPolicyTests(unittest.TestCase):
    setUp = fixture.AlertTests.setUp

    def observe(self, seconds, *, fast=False, unknown=False, **metrics):
        observation = {**HEALTHY, 'host_metrics': {} if unknown else {**HEALTHY['host_metrics'], **metrics}}
        self.driver.inspect_node_services.return_value = observation
        self.driver.host_metrics = Mock(return_value=observation)
        with patch('backend.alerts.datetime') as clock:
            clock.fromisoformat = datetime.fromisoformat
            clock.now.return_value = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)
            return self.service.scheduled(cpu_only=fast)

    def events(self):
        with self.db.connect() as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM backend_alert_events ORDER BY created_at,id').fetchall()]

    def test_off_keeps_one_incident_and_only_one_critical_escalation(self):
        self.service.preferences(self.actor, {'repeat_minutes': 0})
        for index, value in enumerate([86, 96, 89, 96, 89, 96, 89, 96]):
            self.observe(index * 30, cpu_used_percent=value, load1=5, ram_used_percent=92)
        self.observe(10800, cpu_used_percent=96, load1=5, ram_used_percent=92)
        events = self.events()
        self.assertEqual([e['kind'] for e in events].count('cpu_high'), 2)
        self.assertEqual([e['kind'] for e in events].count('load_high'), 1)
        self.assertEqual([e['kind'] for e in events].count('ram_high'), 1)
        self.assertEqual(self.service.overview(self.actor)['active_count'], 3)
        self.observe(10830)
        self.assertEqual(len(self.events()), 4)
        self.observe(10860)
        self.assertEqual(sum(e['resolved'] for e in self.events()), 3)
        self.assertEqual(self.service.overview(self.actor)['active_count'], 0)
        self.observe(10890, cpu_used_percent=96)
        self.assertEqual(len(self.events()), 8, 'a genuinely new incident notifies again')

    def test_due_reminder_uses_latest_value_and_survives_service_restart(self):
        self.service.preferences(self.actor, {'repeat_minutes': 30})
        self.observe(0, load1=5)
        self.observe(1770, load1=6)
        self.assertEqual(len(self.events()), 1)
        self.service = AlertService(self.db, self.driver)
        self.observe(1800, load1=7)
        self.assertEqual(len(self.events()), 2)
        self.assertEqual(json.loads(self.events()[-1]['payload_json'])['value'], 7)
        self.observe(1830, load1=8)
        self.assertEqual(len(self.events()), 2, 'last_seen must not reset the reminder timer')
        self.observe(3600, load1=9)
        self.assertEqual(len(self.events()), 3)
        for event in self.events():
            self.assertFalse(any(key.startswith('_') for key in json.loads(event['payload_json'])))
        self.assertFalse(any(key.startswith('_') for key in self.service.overview(self.actor)['active'][0]['payload']))

    def test_hysteresis_band_does_not_reopen_cpu_la_ram_or_disk(self):
        self.service.preferences(self.actor, {'repeat_minutes': 0})
        self.observe(0, cpu_used_percent=96, load1=5, ram_used_percent=92, disk_free_percent=3)
        self.observe(30, cpu_used_percent=82, load1=3.2, ram_used_percent=87, disk_free_percent=11)
        self.observe(60, cpu_used_percent=96, load1=5, ram_used_percent=92, disk_free_percent=3)
        self.assertEqual(len(self.events()), 4)
        self.assertEqual(self.service.overview(self.actor)['active_count'], 4)
        self.observe(90)
        self.observe(120)
        self.assertEqual(sum(e['resolved'] for e in self.events()), 4)

    def test_unknown_breaks_recovery_confirmation_and_fast_scan_preserves_disk_candidate(self):
        self.observe(0, cpu_used_percent=96, load1=5, ram_used_percent=92, disk_free_percent=3)
        self.observe(30)
        self.observe(45, fast=True)
        self.observe(60, unknown=True)
        self.observe(90)
        self.assertEqual(self.service.overview(self.actor)['active_count'], 4)
        self.observe(105, fast=True)
        self.observe(120)
        self.assertEqual(self.service.overview(self.actor)['active_count'], 0)
        self.assertEqual(sum(e['resolved'] for e in self.events()), 4)
        self.assertEqual(json.loads(next(e for e in self.events() if e['kind'] == 'disk_low' and e['resolved'])['payload_json'])['value'], 50)

    def test_unknown_does_not_remind_and_recovery_is_not_delayed_by_repeat_timer(self):
        self.service.preferences(self.actor, {'repeat_minutes': 30})
        self.observe(0, load1=5)
        self.observe(1800, unknown=True)
        self.assertEqual(len(self.events()), 1)
        self.observe(1830)
        self.observe(1860)
        self.assertEqual([e['resolved'] for e in self.events()], [0, 1])

    def test_legacy_policy_defaults_and_strict_http_choices(self):
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_system_settings SET value=? WHERE key='alert_policy'", (json.dumps({'enabled': True, 'interval_minutes': 3, 'notify_resolved': True}),))
        self.assertEqual(self.service.overview(self.actor)['repeat_minutes'], 60)
        headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101'}
        for value in [0, 30, 60, 180]:
            response = self.client.patch('/api/v1/system/alerts/preferences', headers=headers, json={'repeat_minutes': value})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['repeat_minutes'], value)
        for value in [True, False, 1, 15, -1, '30', 30.0]:
            response = self.client.patch('/api/v1/system/alerts/preferences', headers=headers, json={'repeat_minutes': value})
            self.assertEqual(response.status_code, 422, (value, response.text))
            with self.assertRaises(AccessDenied):
                self.service.preferences(self.actor, {'repeat_minutes': value})

    def test_legacy_active_state_uses_last_event_time_for_reminder(self):
        self.service.preferences(self.actor, {'repeat_minutes': 30})
        self.observe(0, load1=5)
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_alert_state SET payload_json=? WHERE kind='load_high'", (json.dumps({'value': 5, 'cpus': 2}),))
        self.observe(1770, load1=6)
        self.assertEqual(len(self.events()), 1)
        self.observe(1800, load1=7)
        self.assertEqual(len(self.events()), 2)

    def test_fast_path_evaluates_ram_and_la_without_service_inspection(self):
        self.observe(0, fast=True, ram_used_percent=92, load1=5)
        self.driver.inspect_node_services.assert_not_called()
        self.assertEqual({event['kind'] for event in self.events()}, {'ram_high', 'load_high'})

    def test_driver_failure_and_long_gaps_break_healthy_confirmation(self):
        self.observe(0, load1=5)
        self.observe(30)
        self.driver.binary_info.side_effect = RuntimeError('driver unavailable')
        self.observe(60)
        self.driver.binary_info.side_effect = None
        self.observe(90)
        self.assertEqual(self.service.overview(self.actor)['active_count'], 1)
        self.observe(600)
        self.assertEqual(self.service.overview(self.actor)['active_count'], 1)
        self.observe(630)
        self.assertEqual([e['resolved'] for e in self.events()], [0, 1])

    def test_switching_reminders_off_preserves_initial_and_recovery(self):
        self.service.preferences(self.actor, {'repeat_minutes': 30})
        self.observe(0, load1=5)
        self.service.preferences(self.actor, {'repeat_minutes': 0})
        self.observe(1800, load1=6)
        self.assertEqual(len(self.events()), 1)
        self.observe(1830)
        self.observe(1860)
        self.assertEqual([e['resolved'] for e in self.events()], [0, 1])
