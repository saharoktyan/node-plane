"""The web cabinet: a system service apart from the relay.

One row: where the cabinet is served from, `relay` (the relay's node, on
the relay's domain) or `local` (the controller's own server on `listen`),
the public address people open it at (the relay's when empty), which is
also the passkeys' domain, and how far its files are applied. The files
are the controller's build; `applied_version` is the release they came
from.
"""


def create_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_web_cabinet (
        id INTEGER PRIMARY KEY CHECK(id = 1),
        mode TEXT NOT NULL CHECK(mode IN ('relay', 'local')),
        node_key TEXT REFERENCES backend_nodes(key) ON DELETE CASCADE,
        listen TEXT,
        public_url TEXT,
        status TEXT NOT NULL CHECK(status IN ('applying', 'active', 'failed', 'removing')),
        revision INTEGER NOT NULL,
        applied_revision INTEGER,
        applied_version TEXT,
        error_code TEXT,
        attempted_at TEXT,
        actor_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL)''')


def upgrade(conn):
    create_schema(conn)
