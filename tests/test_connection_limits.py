import unittest

from backend.connection_limits import ConnectionLimits
from backend.vpn_policies import VpnPolicies
from tests import test_backend_device_commands


class Driver:
    def __init__(self):
        self.calls, self.in_force, self.error = [], None, None

    def apply_admission_policy(self, node_key, revision, clients):
        if self.error:
            raise self.error
        self.calls.append((node_key, revision, clients))
        return self.in_force if self.in_force is not None else revision


class Unimplemented(Exception):
    def code(self):
        return type('Code', (), {'name': 'UNIMPLEMENTED'})()


class ConnectionLimitTests(unittest.TestCase):
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

    def test_nodes_without_limits_are_never_contacted(self):
        self.assertEqual(self.limits.sync(), [])
        self.assertEqual(self.driver.calls, [])
        self.assertIsNone(self.row())

    def test_snapshot_lists_limited_clients_and_resends_only_new_revisions(self):
        self.installation('connections.mode', 'enforce')
        self.limits.sync()
        expected = {'email': self.email(self.phone), 'mode': 'enforce', 'max_sources': 1,
                    'window_seconds': 120, 'cooldown_seconds': 300}
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

    def test_scheduled_sync_runs_at_most_once_a_minute(self):
        self.installation('connections.mode', 'enforce')
        self.assertTrue(self.limits.scheduled())
        self.assertFalse(self.limits.scheduled())
        self.assertEqual(len(self.driver.calls), 1)
