"""Services on a node: one record per service instance.

A node is a server with an agent; services run on it. VPN is the first,
built-in type: its configuration and revisions stay in `backend_nodes` for
now, as the storage of the node's VPN instance. Later service types keep
their own configuration with their instance.
"""


def create_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_service_instances (
        id TEXT PRIMARY KEY,
        node_key TEXT NOT NULL REFERENCES backend_nodes(key) ON DELETE CASCADE,
        service TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(node_key, service))''')


def backfill(conn):
    """A VPN instance for every node with VPN protocols."""
    from datetime import datetime, timezone
    from uuid import uuid4
    now = datetime.now(timezone.utc).isoformat()
    for row in conn.execute("""SELECT n.key FROM backend_nodes n WHERE n.protocols_json <> '[]'
            AND NOT EXISTS (SELECT 1 FROM backend_service_instances s WHERE s.node_key=n.key AND s.service='vpn')
            ORDER BY n.key""").fetchall():
        conn.execute('INSERT INTO backend_service_instances(id,node_key,service,created_at) VALUES (?,?,?,?)',
                     (str(uuid4()), row['key'], 'vpn', now))


def upgrade(conn):
    create_schema(conn)
    backfill(conn)
