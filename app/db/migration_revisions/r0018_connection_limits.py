"""Concurrent VLESS source limits: per-node delivery state, observed source
counts and eviction events. Runtime state without addresses, not backed up."""


def upgrade(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_node_admission (
        node_key TEXT PRIMARY KEY REFERENCES backend_nodes(key) ON DELETE CASCADE,
        revision BIGINT NOT NULL, digest TEXT NOT NULL,
        applied_revision BIGINT NOT NULL DEFAULT 0,
        status TEXT NOT NULL CHECK(status IN ('pending','current','unavailable','outdated')),
        checked_at TEXT,
        events_boot TEXT NOT NULL DEFAULT '', events_sequence BIGINT NOT NULL DEFAULT 0)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_vpn_session_observations (
        device_id TEXT NOT NULL REFERENCES backend_devices(id) ON DELETE CASCADE,
        node_key TEXT NOT NULL REFERENCES backend_nodes(key) ON DELETE CASCADE,
        active_sources INTEGER NOT NULL, blocked_sources INTEGER NOT NULL,
        observed_at TEXT NOT NULL,
        PRIMARY KEY(device_id,node_key))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_admission_events (
        id TEXT PRIMARY KEY,
        device_id TEXT NOT NULL REFERENCES backend_devices(id) ON DELETE CASCADE,
        node_key TEXT NOT NULL REFERENCES backend_nodes(key) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK(kind IN ('evicted','would_evict')),
        occurred_at TEXT NOT NULL,
        alerted INTEGER NOT NULL DEFAULT 0)''')
    conn.execute('''CREATE INDEX IF NOT EXISTS backend_admission_events_device
        ON backend_admission_events(device_id,occurred_at)''')
