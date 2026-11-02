"""Per-node delivery state of concurrent VLESS source limits; runtime state, not backed up."""


def upgrade(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_node_admission (
        node_key TEXT PRIMARY KEY REFERENCES backend_nodes(key) ON DELETE CASCADE,
        revision BIGINT NOT NULL, digest TEXT NOT NULL,
        applied_revision BIGINT NOT NULL DEFAULT 0,
        status TEXT NOT NULL CHECK(status IN ('pending','current','unavailable','outdated')),
        checked_at TEXT)''')
