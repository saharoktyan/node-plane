"""Services on a node: one record per service instance.

A node is a server with an agent; services run on it. The built-in types are
the VPN engines, one instance per engine: `xray` and `awg`. An engine is one
process with one configuration; the protocols Xray serves (VLESS today) are
its inbounds, not separate services. Their configuration and revisions stay
in `backend_nodes` for now. Later service types keep their own
configuration with their instance.
"""
import json

TYPES = ('xray', 'awg')


def create_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_service_instances (
        id TEXT PRIMARY KEY,
        node_key TEXT NOT NULL REFERENCES backend_nodes(key) ON DELETE CASCADE,
        service TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(node_key, service))''')


def backfill(conn):
    """An instance for every VPN engine a node is configured with."""
    from datetime import datetime, timezone
    from uuid import uuid4
    now = datetime.now(timezone.utc).isoformat()
    for row in conn.execute("SELECT key, protocols_json FROM backend_nodes ORDER BY key").fetchall():
        for service in sorted(set(json.loads(row['protocols_json'])) & set(TYPES)):
            conn.execute('''INSERT INTO backend_service_instances(id,node_key,service,created_at)
                VALUES (?,?,?,?) ON CONFLICT(node_key,service) DO NOTHING''',
                (str(uuid4()), row['key'], service, now))


def upgrade(conn):
    create_schema(conn)
    backfill(conn)
