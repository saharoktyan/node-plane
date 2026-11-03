"""Per-node policy snapshots that agents enforce on their own.

Connection limits and speed limits share one delivery model: the controller
resolves a full snapshot per node, a changed snapshot gets the next
revision, and every pass sends the current one. The agent ignores a
revision that is not newer than its own, so resending is harmless and also
restores a node whose journal was lost. Nodes that never had a snapshot are
never contacted. Each feature keeps its delivery state in its own table
with the columns `node_key, revision, digest, applied_revision, status,
checked_at`.
"""
from datetime import datetime, timezone
import hashlib
import json

from .nodes import AGENT_INSTALLED_SQL


def digest(clients):
    return hashlib.sha256(json.dumps(clients, sort_keys=True).encode()).hexdigest()


def target_nodes(conn):
    """Enabled nodes with an agent that are not being retired or cleaned up."""
    return conn.execute(f'''SELECT n.key,n.protocols_json FROM backend_nodes n
        WHERE n.enabled=1 AND {AGENT_INSTALLED_SQL}
        AND NOT EXISTS (SELECT 1 FROM backend_node_retirements r WHERE r.node_key=n.key)
        AND NOT EXISTS (SELECT 1 FROM backend_node_cleanup c WHERE c.node_key=n.key)
        ORDER BY n.key''').fetchall()


def active_devices(conn, node_key, protocol, now=None):
    """Active devices with access to `protocol` on this node: granted, not
    frozen, suspended by a quota, expired or awaiting deletion, owned by an
    approved account. `name` is the device's peer on the node."""
    from .quotas import BLOCKED_SQL
    now = now or datetime.now(timezone.utc)
    if protocol == 'xray':
        name, join = 'c.runtime_name', "JOIN backend_device_credentials c ON c.device_id=d.id AND c.protocol='xray'"
    else:
        name, join = 'd.runtime_name', ''
    rows = conn.execute(f'''SELECT d.id AS device_id, d.profile_id, p.expires_at, {name} AS name
        FROM backend_devices d {join}
        JOIN backend_profiles p ON p.id=d.profile_id
        JOIN backend_grants g ON g.profile_id=p.id AND g.node_key=? AND g.protocol=?
        LEFT JOIN backend_accounts a ON a.id=p.owner_account_id
        WHERE d.status='active' AND p.frozen=0 AND NOT {BLOCKED_SQL}
        AND (p.owner_account_id IS NULL OR a.status='approved')
        AND NOT EXISTS (SELECT 1 FROM backend_profile_deletions x WHERE x.profile_id=p.id)
        ORDER BY name''', (node_key, protocol)).fetchall()
    return [row for row in rows if not row['expires_at'] or datetime.fromisoformat(row['expires_at']) > now]


def plan(conn, table, snapshot):
    """Records a new revision for every changed snapshot; returns
    `(node_key, revision, clients)` to send. `snapshot(conn, node)` returns
    the node's clients."""
    sends = []
    for node in target_nodes(conn):
        clients = snapshot(conn, node)
        value = digest(clients)
        row = conn.execute(f'SELECT revision,digest FROM {table} WHERE node_key=?', (node['key'],)).fetchone()
        if row is None:
            if not clients:
                continue
            conn.execute(f'''INSERT INTO {table}(node_key,revision,digest,status)
                VALUES (?,1,?,'pending')''', (node['key'], value))
            revision = 1
        elif row['digest'] != value:
            revision = row['revision'] + 1
            conn.execute(f'''UPDATE {table} SET revision=?,digest=?,status='pending'
                WHERE node_key=?''', (revision, value, node['key']))
        else:
            revision = row['revision']
        sends.append((node['key'], revision, clients))
    return sends


def record(conn, table, node_key, sent, in_force, status, **extra):
    """Stores the outcome of sending revision `sent`; `in_force` is what the
    agent holds, None when the call failed. `extra` columns are stored with
    a successful call."""
    now = datetime.now(timezone.utc).isoformat()
    if in_force is not None and in_force > sent:
        # The agent holds a newer revision, e.g. after a controller
        # restore. Move past it; the next pass resends this snapshot.
        conn.execute(f'''UPDATE {table} SET revision=?,status='pending',checked_at=?
            WHERE node_key=? AND revision=?''', (in_force + 1, now, node_key, sent))
    elif in_force == sent:
        columns = ''.join(f',{column}=?' for column in extra)
        conn.execute(f'''UPDATE {table} SET applied_revision=?,status='current',checked_at=?{columns}
            WHERE node_key=? AND revision=?''', (sent, now, *extra.values(), node_key, sent))
    else:
        conn.execute(f'''UPDATE {table} SET status=?,checked_at=?
            WHERE node_key=? AND revision=?''', (status, now, node_key, sent))


def failure_status(exc):
    """'outdated' when the agent does not know the call, else 'unavailable'."""
    code = getattr(exc, 'code', lambda: None)()
    return 'outdated' if getattr(code, 'name', '') == 'UNIMPLEMENTED' else 'unavailable'


def paused(conn):
    """No deliveries during maintenance or a restore."""
    from .maintenance_gate import active
    return active(conn) or conn.execute(
        "SELECT 1 FROM backend_backup_jobs WHERE action='restore' AND status IN ('awaiting_executor','running')"
    ).fetchone() is not None
