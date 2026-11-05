"""Traffic quota periods, exhaustion and resets (VPN plan, section 8)."""
from datetime import datetime, timedelta, timezone
import unittest
from uuid import uuid4
from zoneinfo import ZoneInfo

from backend import quotas
from backend.quotas import QuotaService, period
from backend.vpn_policies import VpnPolicies
from tests import test_backend_device_commands

UTC = timezone.utc


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=UTC) if 'T' in text and '+' not in text else \
        datetime.fromisoformat(text)


class PeriodBoundaryTests(unittest.TestCase):
    def bounds(self, strategy, anchor, now, tz=UTC, days=30):
        start, end = period(strategy, at(anchor), days, tz, at(now))
        return start.isoformat(), end and end.isoformat()

    def test_calendar_month_starts_at_local_midnight(self):
        self.assertEqual(self.bounds('calendar_month', '2026-01-15T10:00', '2026-02-28T23:59'),
                         ('2026-02-01T00:00:00+00:00', '2026-03-01T00:00:00+00:00'))
        moscow = ZoneInfo('Europe/Moscow')
        # 21:00 UTC on the last day is already the next month in UTC+3.
        self.assertEqual(self.bounds('calendar_month', '2026-01-15T10:00', '2026-03-31T21:30', moscow),
                         ('2026-03-31T21:00:00+00:00', '2026-04-30T21:00:00+00:00'))

    def test_monthly_anchor_uses_the_last_day_of_shorter_months(self):
        self.assertEqual(self.bounds('monthly_anchor', '2026-01-31T12:00', '2026-02-15T00:00'),
                         ('2026-01-31T12:00:00+00:00', '2026-02-28T12:00:00+00:00'))
        self.assertEqual(self.bounds('monthly_anchor', '2026-01-31T12:00', '2026-03-01T00:00'),
                         ('2026-02-28T12:00:00+00:00', '2026-03-31T12:00:00+00:00'))
        # Leap year.
        self.assertEqual(self.bounds('monthly_anchor', '2028-01-31T12:00', '2028-02-29T13:00'),
                         ('2028-02-29T12:00:00+00:00', '2028-03-31T12:00:00+00:00'))
        # Exactly on the boundary the new period has started.
        self.assertEqual(self.bounds('monthly_anchor', '2026-01-10T00:00', '2026-05-10T00:00')[0],
                         '2026-05-10T00:00:00+00:00')

    def test_interval_and_no_reset(self):
        self.assertEqual(self.bounds('interval', '2026-01-01T06:00', '2026-01-03T07:00', days=1),
                         ('2026-01-03T06:00:00+00:00', '2026-01-04T06:00:00+00:00'))
        self.assertEqual(self.bounds('interval', '2026-01-01T06:00', '2026-03-05T00:00', days=30),
                         ('2026-03-02T06:00:00+00:00', '2026-04-01T06:00:00+00:00'))
        self.assertEqual(self.bounds('none', '2026-01-01T06:00', '2030-01-01T00:00'),
                         ('2026-01-01T06:00:00+00:00', None))


class QuotaServiceTests(unittest.TestCase):
    setUp_commands = test_backend_device_commands.BackendDeviceCommandTests.setUp
    headers_for = test_backend_device_commands.BackendDeviceCommandTests.headers_for
    create = test_backend_device_commands.BackendDeviceCommandTests.create
    prepare = test_backend_device_commands.BackendDeviceCommandTests.prepare
    grants = test_backend_device_commands.BackendDeviceCommandTests.grants
    add = test_backend_device_commands.BackendDeviceCommandTests.add

    def setUp(self):
        self.setUp_commands()
        VpnPolicies(self.db).initialize_schema()
        self.service = QuotaService(self.db)
        self.service.initialize_schema()
        self.profile = self.prepare()
        # The owner receives service notifications in Telegram.
        owner = self.db.connection.execute('SELECT owner_account_id FROM backend_profiles WHERE id=?',
                                           (self.profile,)).fetchone()[0]
        self.db.connection.execute("INSERT INTO backend_external_identities(provider,subject,account_id) "
                                   "VALUES ('telegram','202',?)", (owner,))
        self.db.connection.execute("INSERT INTO backend_account_channels(account_id,channel,address) "
                                   "VALUES (?,'telegram','202')", (owner,))
        self.grants(self.profile, 1, [{'node_key': 'node', 'protocol': 'awg'}])
        self.add(self.profile, 'Phone', 2)
        self.now = datetime(2026, 3, 10, 12, tzinfo=UTC)

    def policy(self, key, value, profile=False):
        path = f'/api/v1/profiles/{self.profile}/vpn-policies' if profile else '/api/v1/vpn-policies/installation'
        current = self.client.get(path, headers=self.headers_for()).json()
        revision = next(item for item in current['keys'] if item['key'] == key)['revision']
        response = self.client.patch(path, headers=self.headers_for(),
                                     json={'changes': [{'key': key, 'value': value, 'revision': revision}]})
        self.assertEqual(response.status_code, 200, response.text)

    def tick(self, **delta):
        if delta:
            self.now += timedelta(**delta)
        return self.service.scheduled(self.now)

    def use(self, total):
        with self.db.transaction() as conn:
            quotas.accrue(conn, self.profile, total // 2, total - total // 2)

    def open_period(self):
        return self.db.connection.execute(
            'SELECT * FROM backend_quota_periods WHERE profile_id=? AND closed_at IS NULL', (self.profile,)).fetchone()

    def notifications(self):
        return [row[0] for row in self.db.connection.execute(
            "SELECT kind FROM backend_system_notifications WHERE kind LIKE 'quota_%' ORDER BY rowid").fetchall()]

    def latest_actions(self):
        row = self.db.connection.execute('''SELECT id FROM backend_operations WHERE profile_id=?
            ORDER BY desired_revision DESC LIMIT 1''', (self.profile,)).fetchone()
        return {t[0] for t in self.db.connection.execute(
            'SELECT action FROM backend_operation_tasks WHERE operation_id=?', (row[0],)).fetchall()}

    def test_profiles_without_a_quota_get_no_period(self):
        self.tick()
        self.assertIsNone(self.open_period())

    def test_warning_exhaustion_suspension_and_reset_happen_once(self):
        self.policy('quota.bytes', 1000)
        self.tick()
        row = self.open_period()
        self.assertEqual((row['period_start'], row['period_end']),
                         ('2026-03-01T00:00:00+00:00', '2026-04-01T00:00:00+00:00'))
        self.use(850)
        self.tick(minutes=1)
        self.assertEqual(self.notifications(), ['quota_warning'])
        self.use(200)
        self.assertEqual(self.tick(minutes=1), 1)
        self.assertTrue(self.open_period()['blocked'])
        self.assertEqual(self.latest_actions(), {'delete'})
        summary = self.client.get(f'/api/v1/profiles/{self.profile}/summary', headers=self.headers_for())
        self.assertTrue(summary.json()['quota_suspended'], summary.text)
        # A restarted worker changes nothing and notifies nobody again.
        self.assertEqual(self.tick(minutes=1), 0)
        self.assertEqual(self.notifications(), ['quota_warning', 'quota_exhausted'])
        # The next period lifts the suspension.
        self.now = datetime(2026, 4, 1, 0, 0, 30, tzinfo=UTC)
        self.assertEqual(self.tick(), 1)
        self.assertFalse(self.open_period()['blocked'])
        self.assertEqual(self.open_period()['period_start'], '2026-04-01T00:00:00+00:00')
        self.assertEqual(self.latest_actions(), {'ensure'})
        self.assertEqual(self.notifications(), ['quota_warning', 'quota_exhausted', 'quota_reset'])
        closed = self.db.connection.execute('''SELECT uplink_bytes+downlink_bytes,close_reason FROM backend_quota_periods
            WHERE profile_id=? AND closed_at IS NOT NULL''', (self.profile,)).fetchall()
        self.assertEqual([tuple(row) for row in closed], [(1050, 'period_end')])

    def test_notify_only_and_a_raised_limit_keep_access(self):
        self.policy('quota.bytes', 1000)
        self.policy('quota.on_exhausted', 'notify', profile=True)
        self.tick()
        self.use(1200)
        self.tick(minutes=1)
        self.assertFalse(self.open_period()['blocked'])
        self.assertEqual(self.notifications(), ['quota_exhausted'])
        self.policy('quota.on_exhausted', None, profile=True)
        self.tick(minutes=1)
        self.assertTrue(self.open_period()['blocked'])
        # A raised limit applies to the current period at once.
        self.policy('quota.bytes', 5000, profile=True)
        self.tick(minutes=1)
        self.assertFalse(self.open_period()['blocked'])
        self.assertEqual(self.latest_actions(), {'ensure'})

    def test_throttle_keeps_access_and_caps_speed_until_the_reset(self):
        import json
        from backend.speed_limits import SpeedLimits, node_snapshot
        SpeedLimits(self.db).initialize_schema()
        self.policy('quota.bytes', 1000)
        self.policy('quota.on_exhausted', 'throttle')
        self.policy('speed.down_kbit', 500, profile=True)

        def limits():
            with self.db.connect() as conn:
                return [(c['up_kbit'], c['down_kbit']) for c in node_snapshot(conn, 'node', ['awg'], self.now)]
        self.tick()
        self.assertEqual(limits(), [(0, 500)])
        self.use(1000)
        self.tick(minutes=1)
        self.assertFalse(self.open_period()['blocked'])
        # The lower own limit stays; the free direction gets the throttle.
        self.assertEqual(limits(), [(1000, 500)])
        payload = json.loads(self.db.connection.execute(
            "SELECT payload_json FROM backend_system_notifications WHERE kind='quota_exhausted'").fetchone()[0])
        self.assertEqual((payload['suspended'], payload['throttled']), (False, 1000))
        quota = self.client.get(f'/api/v1/profiles/{self.profile}/quota', headers=self.headers_for()).json()
        self.assertEqual(quota['throttled'], 1000)
        summary = self.client.get(f'/api/v1/profiles/{self.profile}/summary', headers=self.headers_for()).json()
        self.assertEqual(summary['quota']['throttled_kbit'], 1000)
        # A raised limit lifts the throttle at once, as does the next period.
        self.policy('quota.bytes', 5000, profile=True)
        self.assertEqual(limits(), [(0, 500)])
        self.policy('quota.bytes', None, profile=True)
        self.assertEqual(limits(), [(1000, 500)])
        self.now = datetime(2026, 4, 1, 0, 0, 30, tzinfo=UTC)
        self.tick()
        self.assertEqual(limits(), [(0, 500)])

    def test_a_new_period_does_not_lift_an_administrators_freeze(self):
        self.policy('quota.bytes', 1000)
        self.tick()
        self.use(1000)
        self.tick(minutes=1)
        self.db.connection.execute('UPDATE backend_profiles SET frozen=1 WHERE id=?', (self.profile,))
        self.now = datetime(2026, 4, 1, 0, 1, tzinfo=UTC)
        self.tick()
        self.assertFalse(self.open_period()['blocked'])
        self.assertEqual(self.latest_actions(), {'delete'})

    def test_a_profile_can_be_exempt_from_the_installation_quota(self):
        self.policy('quota.bytes', 1000)
        self.policy('quota.bytes', 0, profile=True)
        self.tick()
        self.assertIsNone(self.open_period())
        quota = self.client.get(f'/api/v1/profiles/{self.profile}/quota', headers=self.headers_for()).json()
        self.assertIsNone(quota['limit_bytes'])

    def test_removing_the_quota_closes_the_period_and_restores_access(self):
        self.policy('quota.bytes', 1000)
        self.tick()
        self.use(1000)
        self.tick(minutes=1)
        self.policy('quota.bytes', None)
        self.tick(minutes=1)
        self.assertIsNone(self.open_period())
        self.assertEqual(self.latest_actions(), {'ensure'})

    def test_manual_reset_starts_a_period_now_and_replays(self):
        self.policy('quota.bytes', 1000)
        path = f'/api/v1/profiles/{self.profile}/quota'
        key = str(uuid4())
        self.assertEqual(self.client.post(path + '/reset', headers=self.headers_for(None, key)).status_code, 200)
        self.tick()
        self.use(1000)
        self.tick(minutes=1)
        key = str(uuid4())
        reset = self.client.post(path + '/reset', headers=self.headers_for(None, key))
        self.assertEqual(reset.status_code, 200, reset.text)
        body = reset.json()
        self.assertEqual(body['period']['used_bytes'], 0)
        self.assertFalse(body['period']['blocked'])
        self.assertEqual(body['history'][0]['close_reason'], 'manual')
        self.assertEqual(body['history'][0]['used_bytes'], 1000)
        self.assertEqual(self.client.post(path + '/reset', headers=self.headers_for(None, key)).json()['period']['id'],
                         body['period']['id'])
        self.assertEqual(self.latest_actions(), {'ensure'})
        self.assertIn('quota_reset', self.notifications())

    def test_overview_and_anchor_changes(self):
        path = f'/api/v1/profiles/{self.profile}/quota'
        self.assertEqual(self.client.get(path, headers=self.headers_for()).json()['limit_bytes'], None)
        self.policy('quota.bytes', 1000)
        self.policy('quota.reset', 'monthly_anchor')
        self.tick()
        quota = self.client.get(path, headers=self.headers_for())
        self.assertEqual(quota.json()['anchor_at'], self.now.isoformat())
        revision = quota.json()['anchor_revision']
        moved = self.client.patch(path, headers=self.headers_for(revision), json={'anchor_at': '2026-01-31T00:00:00+00:00'})
        self.assertEqual(moved.status_code, 200, moved.text)
        # The current period keeps its end; the next one follows the new anchor.
        self.assertEqual(moved.json()['period']['end'], '2026-04-10T12:00:00+00:00')
        self.now = datetime(2026, 4, 10, 12, 1, tzinfo=UTC)
        self.tick()
        self.assertEqual((self.open_period()['period_start'], self.open_period()['period_end']),
                         ('2026-03-31T00:00:00+00:00', '2026-04-30T00:00:00+00:00'))
        self.assertEqual(self.client.patch(path, headers=self.headers_for(revision),
                                           json={'anchor_at': '2026-01-31T00:00:00+00:00'}).status_code, 412)
        # A plain date is midnight in the quota time zone; a naive time is refused.
        self.policy('quota.timezone', 'Europe/Moscow')
        revision = self.client.get(path, headers=self.headers_for()).json()['anchor_revision']
        moved = self.client.patch(path, headers=self.headers_for(revision), json={'anchor_at': '2026-05-15'})
        self.assertEqual((moved.json()['anchor_at'], moved.json()['anchor_day']), ('2026-05-14T21:00:00+00:00', '2026-05-15'))
        self.assertEqual(self.client.patch(path, headers=self.headers_for(revision + 1),
                                           json={'anchor_at': '2026-05-15T00:00'}).status_code, 422)

    def test_collected_traffic_accrues_into_the_open_period(self):
        from backend.traffic import TrafficService
        self.policy('quota.bytes', 10 ** 9)
        self.tick()
        device = self.db.connection.execute('SELECT id FROM backend_devices WHERE profile_id=?',
                                            (self.profile,)).fetchone()[0]
        target = {'key': (self.profile, 'node', 'awg', device), 'account_id': None}
        with self.db.transaction() as conn:
            for up, down, second in ((1000, 2000, 1), (1100, 2300, 2)):
                TrafficService._record(conn, target, {'identity': 'key', 'epoch': 'a' * 64, 'uplink_bytes': up,
                                                      'downlink_bytes': down, 'inbounds': {}},
                                    f'2026-03-10T12:00:0{second}+00:00')
        # The first sample is the baseline; the second adds its difference.
        self.assertEqual((self.open_period()['uplink_bytes'], self.open_period()['downlink_bytes']), (100, 300))

    def test_invalid_time_zones_are_refused(self):
        path = '/api/v1/vpn-policies/installation'
        current = self.client.get(path, headers=self.headers_for()).json()
        for value in ('Mars/Olympus', '../etc', 3):
            response = self.client.patch(path, headers=self.headers_for(), json={'changes': [
                {'key': 'quota.timezone', 'value': value, 'revision': 0}]})
            self.assertEqual(response.status_code, 422, value)
        self.policy('quota.timezone', 'UTC')
        self.assertTrue(any(item['key'] == 'quota.timezone' for item in current['keys']))


if __name__ == '__main__':
    unittest.main()
