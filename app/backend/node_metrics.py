"""Node resources over time, for the overview's charts.

Every five minutes the controller reads each installed agent's cheap host
metrics (memory, load, CPU, disk and network byte counters) and keeps a
reading. Network counters become a rate between two readings; a smaller
counter means the host restarted, so that interval has no rate. Readings
are kept a week; each finished hour also becomes an average kept a month.
Traffic per day is pruned here too, after a little over a year.
"""
import json
import logging
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from .nodes import AGENT_INSTALLED_SQL

INTERVAL = timedelta(minutes=5)
# A gap longer than this between readings gives no network rate.
MAX_GAP = timedelta(minutes=15)
KEEP_READINGS = timedelta(days=7)
KEEP_HOURS = timedelta(days=30)
KEEP_DAYS = timedelta(days=400)
LAST_SCAN = 'node_metrics_last_scan'
FIELDS = ('cpu_percent', 'load1', 'cpus', 'ram_percent', 'disk_free_percent',
          'net_rx_bytes', 'net_tx_bytes', 'net_rx_bps', 'net_tx_bps')


def _number(metrics, key, maximum=None):
    value = metrics.get(key)
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        return None
    return value if maximum is None or value <= maximum else None


def reading(metrics, previous, at):
    """A stored reading from an agent's metrics and the node's last reading."""
    row = {'cpu_percent': _number(metrics, 'cpu_used_percent', 100), 'load1': _number(metrics, 'load1'),
           'cpus': metrics.get('cpus') if type(metrics.get('cpus')) is int and metrics['cpus'] > 0 else None,
           'ram_percent': _number(metrics, 'ram_used_percent', 100),
           'disk_free_percent': _number(metrics, 'disk_free_percent', 100),
           'net_rx_bytes': metrics.get('net_rx_bytes') if type(metrics.get('net_rx_bytes')) is int else None,
           'net_tx_bytes': metrics.get('net_tx_bytes') if type(metrics.get('net_tx_bytes')) is int else None,
           'net_rx_bps': None, 'net_tx_bps': None}
    if previous is not None:
        seconds = (at - datetime.fromisoformat(previous['at'])).total_seconds()
        if 0 < seconds <= MAX_GAP.total_seconds():
            for direction in ('rx', 'tx'):
                now, before = row[f'net_{direction}_bytes'], previous[f'net_{direction}_bytes']
                if now is not None and before is not None and now >= before:
                    row[f'net_{direction}_bps'] = 8 * (now - before) / seconds
    return row


class NodeMetrics:
    def __init__(self, db, driver=None):
        self.db, self.driver = db, driver

    def initialize_schema(self):
        from db.migration_revisions.r0049_metrics_history import node_metrics
        with self.db.transaction() as conn:
            node_metrics(conn)

    def _observe(self, node_key):
        try:
            return (self.driver.host_metrics(node_key).get('host_metrics') or {})
        except Exception:  # noqa: BLE001 -- an unreachable node has no reading
            logging.getLogger(__name__).info('No host metrics from node %s', node_key)
            return None

    def scheduled(self, now=None):
        """One reading per node every five minutes; True when it ran."""
        now = now or datetime.now(timezone.utc)
        with self.db.connect() as conn:
            from .maintenance_gate import active
            if active(conn):
                return False
            row = conn.execute('SELECT value FROM backend_system_settings WHERE key=?', (LAST_SCAN,)).fetchone()
            if row and datetime.fromisoformat(json.loads(row['value'])) > now - INTERVAL:
                return False
            nodes = [row['key'] for row in conn.execute(
                f'SELECT n.key FROM backend_nodes n WHERE {AGENT_INSTALLED_SQL} ORDER BY n.key')]
        observed = []
        if nodes:
            with ThreadPoolExecutor(max_workers=min(4, len(nodes))) as pool:
                observed = list(pool.map(self._observe, nodes))
        stamp = now.replace(microsecond=0).isoformat()
        with self.db.transaction() as conn:
            conn.execute('''INSERT INTO backend_system_settings(key,value) VALUES (?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value''', (LAST_SCAN, json.dumps(stamp)))
            for node_key, metrics in zip(nodes, observed):
                if metrics is None:
                    continue
                previous = conn.execute('''SELECT * FROM backend_node_metrics
                    WHERE node_key=? AND resolution='5m' ORDER BY at DESC LIMIT 1''', (node_key,)).fetchone()
                values = reading(metrics, previous, now)
                conn.execute(f'''INSERT INTO backend_node_metrics (node_key,resolution,at,{",".join(FIELDS)})
                    VALUES (?,'5m',?,{",".join("?" * len(FIELDS))}) ON CONFLICT DO NOTHING''',
                    (node_key, stamp, *(values[field] for field in FIELDS)))
            self._hours(conn, now)
            conn.execute("DELETE FROM backend_node_metrics WHERE resolution='5m' AND at<?",
                         ((now - KEEP_READINGS).isoformat(),))
            conn.execute("DELETE FROM backend_node_metrics WHERE resolution='1h' AND at<?",
                         ((now - KEEP_HOURS).isoformat(),))
            conn.execute('DELETE FROM backend_traffic_daily WHERE day<?', ((now - KEEP_DAYS).date().isoformat(),))
        return True

    @staticmethod
    def _hours(conn, now):
        """Averages each finished hour that has readings but no average yet."""
        hour = now.replace(minute=0, second=0, microsecond=0)
        # Readings come only while the controller runs, so the last few hours suffice.
        rows = conn.execute('''SELECT * FROM backend_node_metrics m WHERE m.resolution='5m' AND m.at>=? AND m.at<?
            ORDER BY m.node_key, m.at''', ((hour - timedelta(hours=3)).isoformat(), hour.isoformat())).fetchall()
        groups = {}
        for row in rows:
            start = datetime.fromisoformat(row['at']).replace(minute=0, second=0, microsecond=0).isoformat()
            groups.setdefault((row['node_key'], start), []).append(row)
        for (node_key, start), readings in groups.items():
            if conn.execute("SELECT 1 FROM backend_node_metrics WHERE node_key=? AND resolution='1h' AND at=?",
                            (node_key, start)).fetchone():
                continue
            def mean(field):
                values = [row[field] for row in readings if row[field] is not None]
                return sum(values) / len(values) if values else None
            values = {field: mean(field) for field in FIELDS}
            values['cpus'] = max((row['cpus'] for row in readings if row['cpus']), default=None)
            values['net_rx_bytes'] = values['net_tx_bytes'] = None
            conn.execute(f'''INSERT INTO backend_node_metrics (node_key,resolution,at,{",".join(FIELDS)})
                VALUES (?,'1h',?,{",".join("?" * len(FIELDS))})''',
                (node_key, start, *(values[field] for field in FIELDS)))
