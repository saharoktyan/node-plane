"""Service instances on nodes.

VPN is the first, built-in service type. Its instance exists while the node
has VPN protocols, or while the removal of its last protocols has not been
applied on the server yet; its configuration and revisions are the node's
VPN columns (`backend_nodes`).
"""
from datetime import datetime, timezone
import json
from uuid import uuid4


def create_schema(conn):
    from db.migration_revisions.r0033_service_instances import create_schema
    create_schema(conn)


def sync_vpn(conn, node_key):
    """Create or remove the node's VPN instance to match its protocols."""
    from .nodes import NodeService, settings_apply_pending
    node = conn.execute(NodeService._select() + ' WHERE n.key=?', (node_key,)).fetchone()
    if node is None:
        return
    wanted = bool(json.loads(node['protocols_json'])) or settings_apply_pending(node)
    if wanted:
        conn.execute('''INSERT INTO backend_service_instances(id,node_key,service,created_at)
            VALUES (?,?,'vpn',?) ON CONFLICT(node_key,service) DO NOTHING''',
            (str(uuid4()), node_key, datetime.now(timezone.utc).isoformat()))
    else:
        conn.execute("DELETE FROM backend_service_instances WHERE node_key=? AND service='vpn'", (node_key,))


def sync_all(conn):
    for row in conn.execute('SELECT key FROM backend_nodes ORDER BY key').fetchall():
        sync_vpn(conn, row['key'])


def vpn_public(row, installed):
    """The VPN instance of a node row from `NodeService._select`, or None."""
    if not row['vpn_instance_id']:
        return None
    from .nodes import settings_apply_pending
    protocols = json.loads(row['protocols_json'])
    if not installed:
        state = 'planned'
    elif not protocols:
        state = 'removing'
    elif settings_apply_pending(row):
        state = 'changes_pending'
    else:
        state = 'active'
    return {'id': row['vpn_instance_id'], 'service': 'vpn', 'state': state, 'protocols': protocols,
            'xray_transports': json.loads(row['xray_transports_json']),
            'desired_revision': row['desired_revision'], 'applied_revision': row['applied_revision']}
