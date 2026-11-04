"""Token installation of node agents: one pending or finished enrollment per
node. The token is kept only until it is used or expires. Runtime state, not
backed up; a restore clears it.
"""


def upgrade(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_agent_enrollments (
        node_key TEXT PRIMARY KEY REFERENCES backend_nodes(key) ON DELETE CASCADE,
        token TEXT, target TEXT NOT NULL,
        restrict_to_controller INTEGER NOT NULL CHECK(restrict_to_controller IN (0, 1)),
        status TEXT NOT NULL CHECK(status IN ('waiting', 'enrolled', 'expired')),
        error_code TEXT, firewall TEXT, journal_archive TEXT,
        actor_id TEXT NOT NULL, expires_at TEXT NOT NULL, attempted_at TEXT,
        created_at TEXT NOT NULL, completed_at TEXT)''')
