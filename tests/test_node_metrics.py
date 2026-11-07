"""Node resources over time: readings every five minutes, network rates
between readings, hourly averages and pruning."""
import unittest
from datetime import datetime, timedelta, timezone

from backend.node_metrics import NodeMetrics, reading

from tests import test_backend_http as fixture


class Driver:
    def __init__(self):
        self.metrics = {'cpu_used_percent': 40.0, 'load1': 0.5, 'cpus': 2, 'ram_used_percent': 60.0,
                        'disk_free_percent': 30.0, 'net_rx_bytes': 1000, 'net_tx_bytes': 500}
        self.down = False
        self.calls = []

    def host_metrics(self, node_key):
        self.calls.append(node_key)
        if self.down:
            raise ConnectionError('unreachable')
        return {'node_key': node_key, 'host_metrics': dict(self.metrics)}


class NodeMetricsTests(unittest.TestCase):
    def setUp(self):
        fixture.BackendHTTPTests.setUp(self)
        with self.db.transaction() as conn:
            conn.execute('''INSERT INTO backend_nodes(key,title,region,enabled,protocols_json,applied_revision)
                VALUES ('m1','M','EU',1,'[]',1)''')
        self.driver = Driver()
        self.service = NodeMetrics(self.db, self.driver)
        self.start = datetime(2026, 11, 6, 10, 0, tzinfo=timezone.utc)

    def rows(self, resolution='5m'):
        return self.db.connection.execute(
            "SELECT * FROM backend_node_metrics WHERE node_key='m1' AND resolution=? ORDER BY at",
            (resolution,)).fetchall()

    def test_readings_rates_and_hours(self):
        self.assertTrue(self.service.scheduled(self.start))
        # Not again within five minutes.
        self.assertFalse(self.service.scheduled(self.start + timedelta(minutes=2)))
        self.driver.metrics.update(net_rx_bytes=1000 + 300 * 1000, net_tx_bytes=500, cpu_used_percent=60.0)
        self.service.scheduled(self.start + timedelta(minutes=5))
        first, second = self.rows()
        self.assertIsNone(first['net_rx_bps'])
        self.assertEqual((second['net_rx_bps'], second['net_tx_bps'], second['cpu_percent']), (8000.0, 0.0, 60.0))
        # A restart lowers the counters: that interval has no rate.
        self.driver.metrics.update(net_rx_bytes=10)
        self.service.scheduled(self.start + timedelta(minutes=10))
        self.assertIsNone(self.rows()[-1]['net_rx_bps'])
        # An unreachable node has no reading.
        self.driver.down = True
        self.service.scheduled(self.start + timedelta(minutes=15))
        self.assertEqual(len(self.rows()), 3)
        # The next hour averages the finished one.
        self.driver.down = False
        self.service.scheduled(self.start + timedelta(hours=1, minutes=1))
        hour, = self.rows('1h')
        self.assertEqual((hour['at'], round(hour['cpu_percent'], 3)), (self.start.isoformat(), round(160 / 3, 3)))
        # Readings go after a week, averages after a month.
        self.service.scheduled(self.start + timedelta(days=8))
        self.assertEqual(len(self.rows()), 1)
        self.service.scheduled(self.start + timedelta(days=31))
        self.assertEqual(self.rows('1h'), [])

    def test_a_reading_refuses_impossible_values(self):
        row = reading({'cpu_used_percent': 150, 'ram_used_percent': -1, 'load1': float('nan'), 'cpus': 0}, None,
                      self.start)
        self.assertEqual((row['cpu_percent'], row['ram_percent'], row['load1'], row['cpus']), (None, None, None, None))

    def test_agent_only_node_has_history_without_vpn_but_invitation_does_not(self):
        from backend.agent_installations import record
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_nodes SET enabled=0,applied_revision=0 WHERE key='m1'")
            record(conn, 'm1', 'token')
            conn.execute('''INSERT INTO backend_nodes(key,title,region,enabled,protocols_json,applied_revision)
                VALUES ('invitation','Waiting','EU',1,'[]',0)''')
        self.assertTrue(self.service.scheduled(self.start))
        self.assertEqual(self.driver.calls, ['m1'])
        row, = self.rows()
        self.assertEqual((row['cpu_percent'], row['ram_percent']), (40.0, 60.0))
        self.assertEqual(self.db.connection.execute(
            "SELECT COUNT(*) FROM backend_node_metrics WHERE node_key='invitation'").fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
