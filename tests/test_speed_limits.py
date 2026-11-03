"""Speed limits: per-node snapshots, overrides and delivery outcomes."""
import json
import unittest
from uuid import uuid4

from backend.speed_limits import SpeedLimits
from tests import test_connection_limits as fixture


class Driver:
    def __init__(self):
        self.calls, self.error = [], None
        self.protocols = [{'protocol': 'xray', 'state': 'applied', 'detail': ''}]

    def apply_rate_policy(self, node_key, revision, clients):
        if self.error:
            raise self.error
        self.calls.append((node_key, revision, clients))
        return {'revision': revision, 'protocols': self.protocols}


class SpeedLimitTests(fixture.Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        SpeedLimits(self.db).initialize_schema()
        self.driver = Driver()
        self.speed = SpeedLimits(self.db, self.driver)

    def revision(self):
        return self.db.connection.execute('SELECT desired_revision FROM backend_profiles WHERE id=?',
                                          (self.profile,)).fetchone()[0]

    def awg_name(self, device):
        return self.db.connection.execute('SELECT runtime_name FROM backend_devices WHERE id=?',
                                          (device['id'],)).fetchone()[0]

    def device_value(self, device, key, value):
        # Device values have no HTTP route yet; resolution is what matters here.
        self.db.connection.execute(
            "INSERT INTO backend_vpn_policies (id,subject_kind,profile_id,device_id,node_key,protocol,key,"
            "value_json,revision,updated_at) VALUES (?,'device',NULL,?,NULL,NULL,?,?,1,'2026-11-03T00:00:00+00:00')",
            (str(uuid4()), device['id'], key, json.dumps(value)))

    def shaping(self):
        return self.db.connection.execute("SELECT * FROM backend_node_shaping WHERE node_key='node'").fetchone()

    def test_nodes_without_limits_are_never_contacted(self):
        self.assertEqual(self.speed.sync(), [])
        self.assertEqual(self.driver.calls, [])
        self.assertIsNone(self.shaping())

    def test_every_granted_protocol_gets_the_resolved_limits(self):
        self.db.connection.execute('''UPDATE backend_nodes SET protocols_json='["awg","xray"]' WHERE key='node' ''')
        granted = self.grants(self.profile, self.revision(),
                              [{'node_key': 'node', 'protocol': 'xray'}, {'node_key': 'node', 'protocol': 'awg'}])
        self.assertEqual(granted.status_code, 200, granted.text)
        self.installation('speed.down_kbit', 20000)
        self.speed.sync()
        node, revision, clients = self.driver.calls[-1]
        self.assertEqual((node, revision), ('node', 1))
        self.assertEqual(clients, [
            {'protocol': 'awg', 'name': self.awg_name(self.phone), 'up_kbit': 0, 'down_kbit': 20000},
            {'protocol': 'xray', 'name': self.email(self.phone), 'up_kbit': 0, 'down_kbit': 20000}])
        row = self.shaping()
        self.assertEqual((row['status'], row['applied_revision']), ('current', 1))
        # A device value overrides the profile, which overrides the installation.
        laptop = self.add(self.profile, 'Laptop', self.revision()).json()['device']
        self.set_policy(f'/api/v1/profiles/{self.profile}/vpn-policies', 'speed.up_kbit', 5000)
        self.device_value(laptop, 'speed.down_kbit', 0)
        self.speed.sync()
        revision, clients = self.driver.calls[-1][1:]
        self.assertEqual(revision, 2)
        by_name = {(c['protocol'], c['name']): (c['up_kbit'], c['down_kbit']) for c in clients}
        self.assertEqual(by_name[('xray', self.email(self.phone))], (5000, 20000))
        self.assertEqual(by_name[('xray', self.email(laptop))], (5000, 0))
        # Exempt in both directions: the device leaves the snapshot.
        self.device_value(laptop, 'speed.up_kbit', 0)
        self.speed.sync()
        self.assertNotIn(self.email(laptop), {c['name'] for c in self.driver.calls[-1][2]})

    def test_node_values_suspended_profiles_and_lifting(self):
        self.installation('speed.up_kbit', 1000)
        self.speed.sync()
        self.assertEqual(self.driver.calls[-1][2][0]['up_kbit'], 1000)
        # A node value overrides the installation value on that node.
        response = self.client.patch('/api/v1/vpn-policies/installation', headers=self.headers_for(), json={
            'changes': [{'key': 'speed.up_kbit', 'value': 3000, 'revision': 0, 'node_key': 'node'}]})
        self.assertEqual(response.status_code, 200, response.text)
        self.speed.sync()
        self.assertEqual(self.driver.calls[-1][1:], (2, [{'protocol': 'xray', 'name': self.email(self.phone),
                                                          'up_kbit': 3000, 'down_kbit': 0}]))
        self.db.connection.execute('UPDATE backend_profiles SET frozen=1 WHERE id=?', (self.profile,))
        self.speed.sync()
        # An empty snapshot lifts every limit on the node.
        self.assertEqual(self.driver.calls[-1][1:], (3, []))

    def test_outcomes_and_failures_are_listed_per_node(self):
        self.installation('speed.up_kbit', 1000)
        self.driver.protocols = [{'protocol': 'xray', 'state': 'unsupported', 'detail': 'nft is not installed'}]
        self.speed.sync()
        listed = self.client.get('/api/v1/speed-limits/nodes', headers=self.headers_for())
        self.assertEqual(listed.status_code, 200, listed.text)
        item = listed.json()['items'][0]
        self.assertEqual((item['node_key'], item['status'], item['protocols']),
                         ('node', 'current', self.driver.protocols))
        self.driver.error = fixture.Unimplemented()
        self.installation('speed.up_kbit', 2000)
        self.speed.sync()
        row = self.shaping()
        # The last known outcome stays next to the failed delivery.
        self.assertEqual((row['status'], row['revision'], row['applied_revision']), ('outdated', 2, 1))
        self.assertIn('unsupported', row['protocols_json'])

    def test_policy_values_are_validated(self):
        current = self.client.get('/api/v1/vpn-policies/installation', headers=self.headers_for()).json()
        revision = next(item for item in current['keys'] if item['key'] == 'speed.up_kbit')['revision']
        for bad in (-1, 100_000_001, 1.5, '1000'):
            response = self.client.patch('/api/v1/vpn-policies/installation', headers=self.headers_for(),
                json={'changes': [{'key': 'speed.up_kbit', 'value': bad, 'revision': revision}]})
            self.assertEqual(response.status_code, 422, bad)
