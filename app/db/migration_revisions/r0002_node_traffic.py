"""Monthly, anonymous node totals; compatible with baseline application readers."""
from datetime import datetime, timezone


def upgrade(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_node_traffic (
        node_key TEXT NOT NULL REFERENCES backend_nodes(key) ON DELETE CASCADE,
        protocol TEXT NOT NULL CHECK(protocol IN ('awg','xray')),
        period_month TEXT NOT NULL,
        uplink_bytes BIGINT NOT NULL, downlink_bytes BIGINT NOT NULL,
        tracked_since TEXT NOT NULL, last_sample_at TEXT NOT NULL,
        PRIMARY KEY(node_key,protocol))''')
    conn.execute('''INSERT INTO backend_node_traffic
        (node_key,protocol,period_month,uplink_bytes,downlink_bytes,tracked_since,last_sample_at)
        SELECT node_key,protocol,period_month,SUM(uplink_bytes),SUM(downlink_bytes),
            MIN(tracked_since),MAX(last_sample_at)
        FROM backend_traffic_usage WHERE tracked_since IS NOT NULL
            AND last_sample_at IS NOT NULL AND period_month=?
        GROUP BY node_key,protocol,period_month
        ON CONFLICT(node_key,protocol) DO NOTHING''',
        (datetime.now(timezone.utc).strftime('%Y-%m'),))
