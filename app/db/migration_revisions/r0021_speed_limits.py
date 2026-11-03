"""Speed limits: per-node delivery state and the agent's latest outcome per
protocol. Runtime state, not backed up; older controllers ignore it."""


def upgrade(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_node_shaping (
        node_key TEXT PRIMARY KEY REFERENCES backend_nodes(key) ON DELETE CASCADE,
        revision BIGINT NOT NULL, digest TEXT NOT NULL,
        applied_revision BIGINT NOT NULL DEFAULT 0,
        status TEXT NOT NULL CHECK(status IN ('pending','current','unavailable','outdated')),
        checked_at TEXT,
        protocols_json TEXT NOT NULL DEFAULT '[]')''')
