"""History for charts: traffic per day (profile × node × protocol), when a
device last moved traffic (who is online), and node resources every five
minutes, kept as hourly averages for longer. None of it is restored from a
backup; it starts again after a restore, like the traffic counters."""


def _column(conn, table, name, kind):
    if getattr(conn, 'backend_name', '') == 'postgres':
        conn.execute(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {kind}')
        return
    if name not in {row['name'] for row in conn.execute(f'PRAGMA table_info({table})').fetchall()}:
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {kind}')


def traffic_history(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_traffic_daily (
        day TEXT NOT NULL,
        profile_id TEXT NOT NULL REFERENCES backend_profiles(id) ON DELETE CASCADE,
        node_key TEXT NOT NULL REFERENCES backend_nodes(key) ON DELETE CASCADE,
        protocol TEXT NOT NULL CHECK(protocol IN ('awg','xray')),
        uplink_bytes BIGINT NOT NULL DEFAULT 0, downlink_bytes BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY(day, profile_id, node_key, protocol))''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_traffic_daily_profile ON backend_traffic_daily(profile_id, day)')
    _column(conn, 'backend_traffic_peers', 'last_active_at', 'TEXT')


def node_metrics(conn):
    # `resolution` is '5m' for a reading, '1h' for an hour's average.
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_node_metrics (
        node_key TEXT NOT NULL REFERENCES backend_nodes(key) ON DELETE CASCADE,
        resolution TEXT NOT NULL CHECK(resolution IN ('5m','1h')),
        at TEXT NOT NULL,
        cpu_percent DOUBLE PRECISION, load1 DOUBLE PRECISION, cpus INTEGER,
        ram_percent DOUBLE PRECISION, disk_free_percent DOUBLE PRECISION,
        net_rx_bytes BIGINT, net_tx_bytes BIGINT,
        net_rx_bps DOUBLE PRECISION, net_tx_bps DOUBLE PRECISION,
        PRIMARY KEY(node_key, resolution, at))''')


def upgrade(conn):
    traffic_history(conn)
    node_metrics(conn)
