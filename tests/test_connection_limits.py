from datetime import datetime, timezone
import unittest

from backend.connection_limits import ConnectionLimits
from backend.vpn_policies import VpnPolicies
from tests import test_backend_device_commands


class Driver:
    def __init__(self):
        self.calls, self.in_force, self.error = [], None, None
        self.observed = {'boot_id': 'boot-1', 'truncated': False, 'events': [], 'sessions': []}
        self.cursors, self.observe_error = [], None

    def observe_sessions(self, node_key, boot_id, after_sequence):
        if self.observe_error:
            raise self.observe_error
        self.cursors.append((node_key, boot_id, after_sequence))
        return self.observed

    def apply_admission_policy(self, node_key, revision, clients):
        if self.error:
            raise self.error
        self.calls.append((node_key, revision, clients))
        return self.in_force if self.in_force is not None else revision


class Unimplemented(Exception):
    def code(self):
        return type('Code', (), {'name': 'UNIMPLEMENTED'})()


class Fixture:
    setUp_devices = test_backend_device_commands.BackendDeviceCommandTests.setUp
    headers_for = test_backend_device_commands.BackendDeviceCommandTests.headers_for
    create = test_backend_device_commands.BackendDeviceCommandTests.create
    prepare = test_backend_device_commands.BackendDeviceCommandTests.prepare
    grants = test_backend_device_commands.BackendDeviceCommandTests.grants
    add = test_backend_device_commands.BackendDeviceCommandTests.add

    def setUp(self):
        self.setUp_devices()
        VpnPolicies(self.db).initialize_schema()
        ConnectionLimits(self.db).initialize_schema()
        self.profile = self.prepare()
        # An installed, enabled node; prepare() creates it without an agent.
        self.db.connection.execute("UPDATE backend_nodes SET enabled=1,applied_revision=1 WHERE key='node'")
        self.grants(self.profile, 1, [{'node_key': 'node', 'protocol': 'xray'}])
        self.phone = self.add(self.profile, 'Phone', 2).json()['device']
        self.driver = Driver()
        self.limits = ConnectionLimits(self.db, self.driver)

    def set_policy(self, path, key, value):
        current = self.client.get(path, headers=self.headers_for()).json()
        revision = next(item for item in current['keys'] if item['key'] == key)['revision']
        response = self.client.patch(path, headers=self.headers_for(),
            json={'changes': [{'key': key, 'value': value, 'revision': revision}]})
        self.assertEqual(response.status_code, 200, response.text)

    def installation(self, key, value):
        self.set_policy('/api/v1/vpn-policies/installation', key, value)

    def row(self):
        return self.db.connection.execute("SELECT * FROM backend_node_admission WHERE node_key='node'").fetchone()

    def email(self, device):
        return self.db.connection.execute('SELECT runtime_name FROM backend_device_credentials WHERE device_id=?',
                                          (device['id'],)).fetchone()[0]


class ConnectionLimitTests(Fixture, unittest.TestCase):

    def test_nodes_without_limits_are_never_contacted(self):
        self.assertEqual(self.limits.sync(), [])
        self.assertEqual(self.driver.calls, [])
        self.assertIsNone(self.row())

    def test_snapshot_lists_limited_clients_and_resends_only_new_revisions(self):
        self.installation('connections.mode', 'enforce')
        self.limits.sync()
        expected = {'email': self.email(self.phone), 'mode': 'enforce', 'max_sources': 1,
                    'window_seconds': 120, 'cooldown_seconds': 30}
        self.assertEqual(self.driver.calls, [('node', 1, [expected])])
        self.assertEqual((self.row()['status'], self.row()['applied_revision']), ('current', 1))
        # Unchanged policy: the same revision again, which the agent ignores.
        self.limits.sync()
        self.assertEqual(self.driver.calls[-1][1], 1)
        laptop = self.add(self.profile, 'Laptop', 3).json()['device']
        self.installation('connections.max_sources', 2)
        self.limits.sync()
        revision, clients = self.driver.calls[-1][1:]
        self.assertEqual(revision, 2)
        self.assertEqual(sorted(c['email'] for c in clients), sorted([self.email(self.phone), self.email(laptop)]))
        self.assertEqual({c['max_sources'] for c in clients}, {2})

    def test_profile_override_device_deletion_and_freeze_remove_clients(self):
        self.installation('connections.mode', 'observe')
        self.limits.sync()
        self.assertEqual(len(self.driver.calls[-1][2]), 1)
        self.set_policy(f'/api/v1/profiles/{self.profile}/vpn-policies', 'connections.mode', 'off')
        self.limits.sync()
        # An empty snapshot lifts every limit and block on the node.
        self.assertEqual(self.driver.calls[-1][1:], (2, []))
        self.set_policy(f'/api/v1/profiles/{self.profile}/vpn-policies', 'connections.mode', 'enforce')
        self.limits.sync()
        self.assertEqual(self.driver.calls[-1][2][0]['mode'], 'enforce')
        self.db.connection.execute('UPDATE backend_profiles SET frozen=1 WHERE id=?', (self.profile,))
        self.limits.sync()
        self.assertEqual(self.driver.calls[-1][2], [])

    def test_an_agent_ahead_of_the_controller_gets_a_newer_revision(self):
        self.installation('connections.mode', 'enforce')
        self.driver.in_force = 7
        self.limits.sync()
        self.assertEqual((self.row()['revision'], self.row()['status']), (8, 'pending'))
        self.driver.in_force = None
        self.limits.sync()
        self.assertEqual(self.driver.calls[-1][1], 8)
        self.assertEqual((self.row()['applied_revision'], self.row()['status']), (8, 'current'))

    def test_failures_are_recorded_and_old_agents_are_named(self):
        self.installation('connections.mode', 'enforce')
        self.driver.error = Unimplemented()
        self.limits.sync()
        self.assertEqual(self.row()['status'], 'outdated')
        self.driver.error = ConnectionError()
        self.limits.sync()
        self.assertEqual((self.row()['status'], self.row()['applied_revision']), ('unavailable', 0))
        listed = self.client.get('/api/v1/connection-limits/nodes', headers=self.headers_for())
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual([(item['node_key'], item['status'], item['revision'], item['applied_revision'])
                          for item in listed.json()['items']], [('node', 'unavailable', 1, 0)])

    def test_scheduled_sync_runs_at_most_once_a_minute(self):
        self.installation('connections.mode', 'enforce')
        self.assertTrue(self.limits.scheduled())
        self.assertFalse(self.limits.scheduled())
        self.assertEqual(len(self.driver.calls), 1)


class ObservationTests(Fixture, unittest.TestCase):
    """Eviction events and source counts read back from the node."""

    def setUp(self):
        super().setUp()
        owner = self.db.connection.execute("""SELECT a.id FROM backend_accounts a
            JOIN backend_external_identities i ON i.account_id=a.id AND i.provider='telegram'
            WHERE a.role='admin' LIMIT 1""").fetchone()[0]
        self.db.connection.execute('UPDATE backend_profiles SET owner_account_id=? WHERE id=?', (owner, self.profile))
        self.installation('connections.mode', 'enforce')

    def evicted(self, sequence, kind='evicted', at=None):
        at = at or int(datetime.now(timezone.utc).timestamp())
        return {'sequence': sequence, 'email': self.email(self.phone), 'kind': kind, 'occurred_at': at}

    def notifications(self):
        return self.db.connection.execute(
            "SELECT device_id,payload_json FROM backend_system_notifications WHERE kind='connection_evicted'").fetchall()

    def alerts(self):
        return self.db.connection.execute(
            "SELECT payload_json FROM backend_alert_events WHERE kind='link_sharing'").fetchall()

    def test_counts_replace_the_previous_observation_and_unknown_clients_are_ignored(self):
        self.driver.observed['sessions'] = [
            {'email': self.email(self.phone), 'active_sources': 1, 'blocked_sources': 1},
            {'email': 'stranger', 'active_sources': 3, 'blocked_sources': 0}]
        self.limits.sync()
        rows = self.db.connection.execute(
            'SELECT device_id,node_key,active_sources,blocked_sources FROM backend_vpn_session_observations').fetchall()
        self.assertEqual([tuple(row) for row in rows], [(self.phone['id'], 'node', 1, 1)])
        devices = self.client.get(f'/api/v1/profiles/{self.profile}/devices', headers=self.headers_for()).json()
        self.assertEqual(next(d for d in devices['items'] if d['id'] == self.phone['id'])['networks'],
                         {'active': 1, 'blocked': 1, 'limit': 1})
        self.driver.observed['sessions'] = []
        self.limits.sync()
        devices = self.client.get(f'/api/v1/profiles/{self.profile}/devices', headers=self.headers_for()).json()
        self.assertEqual(next(d for d in devices['items'] if d['id'] == self.phone['id'])['networks'],
                         {'active': 0, 'blocked': 0, 'limit': 1})
        self.set_policy(f'/api/v1/profiles/{self.profile}/vpn-policies', 'connections.mode', 'off')
        devices = self.client.get(f'/api/v1/profiles/{self.profile}/devices', headers=self.headers_for()).json()
        self.assertTrue(all(d['networks'] is None for d in devices['items']))
        self.assertEqual(self.db.connection.execute(
            'SELECT COUNT(*) FROM backend_vpn_session_observations').fetchone()[0], 0)


    def test_a_device_shows_its_busiest_node_since_the_limit_holds_on_each(self):
        node = dict(self.db.connection.execute("SELECT * FROM backend_nodes WHERE key='node'").fetchone())
        node['key'] = 'other'
        self.db.connection.execute(f"INSERT INTO backend_nodes({','.join(node)}) VALUES ({','.join('?' * len(node))})",
                                   tuple(node.values()))
        # Another node lets this device in from three networks.
        self.db.connection.execute("""INSERT INTO backend_vpn_policies(id,subject_kind,node_key,key,value_json,updated_at)
            VALUES ('wide','installation','other','connections.max_sources','3',?)""",
            (datetime.now(timezone.utc).isoformat(),))
        now = datetime.now(timezone.utc).isoformat()
        for key, active in (('node', 1), ('other', 2)):
            self.db.connection.execute("""INSERT INTO backend_vpn_session_observations
                VALUES (?,?,?,0,?)""", (self.phone['id'], key, active, now))
        devices = self.client.get(f'/api/v1/profiles/{self.profile}/devices', headers=self.headers_for()).json()
        # Not 3 of 1 summed: one of one on the first node is the fuller one.
        self.assertEqual(next(d for d in devices['items'] if d['id'] == self.phone['id'])['networks'],
                         {'active': 1, 'blocked': 0, 'limit': 1})

    def test_an_eviction_notifies_the_owner_once_and_the_cursor_moves(self):
        self.driver.observed['events'] = [self.evicted(1), self.evicted(2)]
        self.limits.sync()
        self.assertEqual(self.driver.cursors[-1], ('node', '', 0))
        notified = self.notifications()
        self.assertEqual(len(notified), 1)
        self.assertEqual(notified[0]['device_id'], self.phone['id'])
        self.assertIn('Phone', notified[0]['payload_json'])
        self.assertNotIn('127.', notified[0]['payload_json'])
        # A resent event is stored once; the next read starts after it.
        self.limits.sync()
        self.assertEqual(self.driver.cursors[-1], ('node', 'boot-1', 2))
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_admission_events').fetchone()[0], 2)
        # A restarted agent numbers from 1 again.
        self.driver.observed = {'boot_id': 'boot-2', 'truncated': False, 'events': [self.evicted(1)], 'sessions': []}
        self.limits.sync()
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_admission_events').fetchone()[0], 3)
        self.assertEqual(len(self.notifications()), 1)
        # Observe mode and old events notify nobody.
        self.driver.observed['events'] = [self.evicted(2, 'would_evict'), self.evicted(3, at=1000)]
        self.limits.sync()
        self.assertEqual(len(self.notifications()), 1)

    def test_frequent_evictions_alert_administrators_once_per_batch(self):
        self.driver.observed['events'] = [self.evicted(n, 'would_evict') for n in range(1, 5)]
        self.limits.sync()
        self.assertEqual(self.alerts(), [])
        self.driver.observed['events'] = [self.evicted(5, 'would_evict')]
        self.limits.sync()
        alerts = self.alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn('"events": 5', alerts[0]['payload_json'])
        self.assertIn('"observe": true', alerts[0]['payload_json'])
        self.driver.observed['events'] = [self.evicted(n) for n in range(6, 10)]
        self.limits.sync()
        self.assertEqual(len(self.alerts()), 1)
        self.driver.observed['events'] = [self.evicted(10)]
        self.limits.sync()
        self.assertEqual(len(self.alerts()), 2)

    def test_an_unreadable_node_keeps_its_limits_current(self):
        self.driver.observe_error = ConnectionError()
        self.limits.sync()
        self.assertEqual(self.row()['status'], 'current')
