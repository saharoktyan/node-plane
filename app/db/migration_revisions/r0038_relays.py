"""The web cabinet's relay: a service instance on a node.

One row per relay instance with its settings and how far they are applied.
The node's agent runs the relay; the controller's driver pins its key
(`relay_key`, hex) and holds the link. One relay per installation for now,
a rule of the service, not of the schema.
"""


def create_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_relays (
        instance_id TEXT PRIMARY KEY REFERENCES backend_service_instances(id) ON DELETE CASCADE,
        node_key TEXT NOT NULL UNIQUE REFERENCES backend_nodes(key) ON DELETE CASCADE,
        domain TEXT NOT NULL,
        port INTEGER NOT NULL,
        tls TEXT NOT NULL CHECK(tls IN ('acme', 'files', 'proxy')),
        acme_contact TEXT,
        certificate_path TEXT,
        key_path TEXT,
        status TEXT NOT NULL CHECK(status IN ('applying', 'active', 'failed', 'removing')),
        revision INTEGER NOT NULL,
        applied_revision INTEGER,
        relay_key TEXT,
        error_code TEXT,
        attempted_at TEXT,
        actor_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL)''')


def upgrade(conn):
    create_schema(conn)
