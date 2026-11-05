"""Service instances on nodes.

The built-in types are the VPN engines `xray` and `awg`, one instance per
engine on a node. An instance exists while the node has that protocol, and
until the removal of the protocol is applied on the server. Xray's protocols
are the inbounds of its instance (VLESS over each configured transport);
a new Xray protocol is a new kind of inbound, not a new service.

The engines' configuration and revisions are the node's VPN columns
(`backend_nodes`) for now, shared by both instances.
"""
from datetime import datetime, timezone
import json
from uuid import uuid4

from db.migration_revisions.r0033_service_instances import TYPES

# Columns `NodeService._select` adds for the instances of a node row.
SELECT_SQL = ',\n            '.join(
    f"(SELECT s.id FROM backend_service_instances s WHERE s.node_key=n.key AND s.service='{service}') "
    f"AS {service}_instance_id" for service in TYPES)


def create_schema(conn):
    from db.migration_revisions.r0033_service_instances import create_schema
    create_schema(conn)


def sync_node(conn, node_key):
    """Create or remove the node's engine instances to match its protocols."""
    from .nodes import NodeService, applied_protocols, settings_apply_pending
    node = conn.execute(NodeService._select() + ' WHERE n.key=?', (node_key,)).fetchone()
    if node is None:
        return
    existing = {row['service'] for row in conn.execute(
        'SELECT service FROM backend_service_instances WHERE node_key=?', (node_key,)).fetchall()}
    wanted = set(json.loads(node['protocols_json'])) & set(TYPES)
    if settings_apply_pending(node):
        # A removed protocol stays on the server until the change is applied;
        # when it is unknown what the server runs, nothing is dropped yet.
        applied = applied_protocols(node)
        wanted |= set(applied) if applied is not None else existing
    now = datetime.now(timezone.utc).isoformat()
    for service in sorted(wanted - existing):
        conn.execute('''INSERT INTO backend_service_instances(id,node_key,service,created_at)
            VALUES (?,?,?,?) ON CONFLICT(node_key,service) DO NOTHING''', (str(uuid4()), node_key, service, now))
    for service in sorted(existing - wanted):
        conn.execute('DELETE FROM backend_service_instances WHERE node_key=? AND service=?', (node_key, service))


def sync_all(conn):
    for row in conn.execute('SELECT key FROM backend_nodes ORDER BY key').fetchall():
        sync_node(conn, row['key'])


def inbounds(service, xray_transports):
    """What the instance serves: Xray's protocols, each over its transport."""
    if service == 'xray':
        return [{'protocol': 'vless', 'transport': transport} for transport in xray_transports]
    return []


def public(row, installed):
    """The instances of a node row from `NodeService._select`."""
    from .nodes import settings_apply_pending
    protocols = json.loads(row['protocols_json'])
    transports = json.loads(row['xray_transports_json'])
    pending = settings_apply_pending(row)
    result = []
    for service in TYPES:
        identity = row[f'{service}_instance_id']
        if not identity:
            continue
        if not installed:
            state = 'planned'
        elif service not in protocols:
            state = 'removing'
        elif pending:
            state = 'changes_pending'
        else:
            state = 'active'
        result.append({'id': identity, 'service': service, 'state': state,
                       'inbounds': inbounds(service, transports if service in protocols else []),
                       'desired_revision': row['desired_revision'], 'applied_revision': row['applied_revision']})
    return result
