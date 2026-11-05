"""Service instances on nodes.

The built-in types are the VPN engines `xray` and `awg`, one instance per
engine on a node. An instance exists while the node has that protocol, and
until the removal of the protocol is applied on the server. Xray's protocols
are the inbounds of its instance (VLESS Reality over TCP and XHTTP today);
a new Xray protocol is a new kind of inbound, not a new service.

The engines' configuration and revisions are the node's VPN columns
(`backend_nodes`) for now. The agent applies the node's configuration as a
whole; a change is pending, and a settings task works, only for the engines
it touches.
"""
from datetime import datetime, timezone
import json
from uuid import uuid4

from db.migration_revisions.r0033_service_instances import TYPES

# System services a node can run besides the VPN engines; their instances
# are set up on their own, not by the node's protocols.
SYSTEM_TYPES = ('relay',)
ALL_TYPES = TYPES + SYSTEM_TYPES

# Columns `NodeService._select` adds for the instances of a node row.
SELECT_SQL = ',\n            '.join(
    [f"(SELECT s.id FROM backend_service_instances s WHERE s.node_key=n.key AND s.service='{service}') "
     f"AS {service}_instance_id" for service in ALL_TYPES]
    + ["(SELECT status FROM backend_relays WHERE node_key=n.key) AS relay_status"])

# A relay's status as the state of its instance.
RELAY_STATES = {'applying': 'changes_pending', 'active': 'active', 'failed': 'failed', 'removing': 'removing'}


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
        'SELECT service FROM backend_service_instances WHERE node_key=?', (node_key,)).fetchall()} & set(TYPES)
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


# A grant (node, protocol) is access to the node's instance of that engine;
# the pair is the instance's key and outlives the instance record.
GRANT_INSTANCE_SQL = '''(SELECT s.id FROM backend_service_instances s
    WHERE s.node_key = g.node_key AND s.service = g.protocol) AS service_instance_id'''


def grant(row):
    return {'node_key': row['node_key'], 'protocol': row['protocol'],
            'service_instance_id': row['service_instance_id']}


def require_manage(actor, services):
    """The actor may manage every one of these service types."""
    from .authorization import require_permission, service_permission
    for service in sorted(services):
        require_permission(actor, service_permission(service))


def sync_all(conn):
    for row in conn.execute('SELECT key FROM backend_nodes ORDER BY key').fetchall():
        sync_node(conn, row['key'])


# Settings owned by one engine; any other setting is shared by the node.
ENGINE_PREFIX = {'xray': 'xray_', 'awg': 'awg_'}


def changed_engines(previous, protocols, settings, *, keys=None):
    """The engines a configuration change touches: added or removed ones, and
    those whose own settings differ; a changed shared setting touches all.
    `previous` is the applied intent (None when unknown); `keys` limits the
    settings compared."""
    now = set(protocols) & set(TYPES)
    if previous is None:
        return now
    before = set(previous.get('protocols') or []) & set(TYPES)
    changed = before ^ now
    old = previous.get('settings') or {}
    for key in set(old) | set(settings):
        if (keys is None or key in keys) and old.get(key) != settings.get(key):
            owner = next((engine for engine, prefix in ENGINE_PREFIX.items() if key.startswith(prefix)), None)
            changed |= {owner} if owner else before | now
    return changed


def pending_engines(row):
    """Engines of a node row whose desired configuration is not applied yet."""
    from .nodes import _SETTINGS, protocol_defaults, settings_apply_pending
    if not settings_apply_pending(row):
        return set()
    protocols = json.loads(row['protocols_json'])
    raw = row['last_runtime_intent_json']
    previous = json.loads(raw) if raw else None
    settings = protocol_defaults(json.loads(row['settings_json']), protocols)
    # VPN policy values are resolved only when the change is queued; a
    # pending change that touches none of the node's own settings is
    # pending for all of its engines.
    return changed_engines(previous, protocols, settings, keys=_SETTINGS) or set(protocols) & set(TYPES)


def inbounds(service, row):
    """What the instance serves. The agent runs both Xray inbounds (VLESS
    Reality over TCP and over XHTTP); `offered` is whether configurations for
    it are issued to people."""
    if service != 'xray':
        return []
    from .nodes import protocol_defaults
    protocols = json.loads(row['protocols_json'])
    settings = protocol_defaults(json.loads(row['settings_json']), protocols)
    offered = set(json.loads(row['xray_transports_json']))
    return [{'protocol': 'vless', 'transport': transport, 'port': settings.get(f'xray_{transport}_port'),
             'offered': transport in offered} for transport in ('tcp', 'xhttp')]


def public(row, installed):
    """The instances of a node row from `NodeService._select`."""
    protocols = json.loads(row['protocols_json'])
    pending = pending_engines(row) if installed else set()
    result = []
    for service in ALL_TYPES:
        identity = row[f'{service}_instance_id']
        if not identity:
            continue
        if service == 'relay':
            result.append({'id': identity, 'service': service, 'state': RELAY_STATES[row['relay_status']],
                           'inbounds': [], 'desired_revision': None, 'applied_revision': None})
            continue
        if not installed:
            state = 'planned'
        elif service not in protocols:
            state = 'removing'
        elif service in pending:
            state = 'changes_pending'
        else:
            state = 'active'
        result.append({'id': identity, 'service': service, 'state': state,
                       'inbounds': inbounds(service, row) if service in protocols else [],
                       'desired_revision': row['desired_revision'], 'applied_revision': row['applied_revision']})
    return result
