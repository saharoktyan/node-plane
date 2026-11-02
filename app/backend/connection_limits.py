"""Concurrent VLESS source limits: resolve them per client and keep agents current.

The agent enforces a full per-node snapshot and journals it, so limits work
while the controller is unreachable (VPN plan, section 5). The controller
resolves `connections.*` for every active VLESS client on a node and sends
the snapshot every minute. The agent ignores a snapshot whose revision is
not newer than its own, so resending is harmless and also restores a node
whose journal was lost. Nodes that never had a limit are never contacted.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging

from .nodes import AGENT_INSTALLED_SQL
from .vpn_policies import effective

INTERVAL_SECONDS = 60
KEYS = ('connections.mode', 'connections.max_sources', 'connections.window_seconds',
        'connections.cooldown_seconds')
LAST_SYNC = 'connection_limits_last_sync'


def create_schema(conn):
    from db.migration_revisions.r0018_connection_limits import upgrade
    upgrade(conn)


def node_snapshot(conn, node_key, now=None):
    """Limited VLESS clients of active devices with a grant on this node."""
    now = now or datetime.now(timezone.utc)
    rows = conn.execute('''SELECT d.id AS device_id, p.expires_at, c.runtime_name
        FROM backend_devices d
        JOIN backend_device_credentials c ON c.device_id=d.id AND c.protocol='xray'
        JOIN backend_profiles p ON p.id=d.profile_id
        JOIN backend_grants g ON g.profile_id=p.id AND g.node_key=? AND g.protocol='xray'
        LEFT JOIN backend_accounts a ON a.id=p.owner_account_id
        WHERE d.status='active' AND p.frozen=0 AND (p.owner_account_id IS NULL OR a.status='approved')
        AND NOT EXISTS (SELECT 1 FROM backend_profile_deletions x WHERE x.profile_id=p.id)
        ORDER BY c.runtime_name''', (node_key,)).fetchall()
    clients = []
    for row in rows:
        if row['expires_at'] and datetime.fromisoformat(row['expires_at']) <= now:
            continue
        values = effective(conn, device_id=row['device_id'], node_key=node_key, protocol='xray', keys=KEYS)
        if values['connections.mode'] == 'off':
            continue
        clients.append({'email': row['runtime_name'], 'mode': values['connections.mode'],
                        'max_sources': values['connections.max_sources'],
                        'window_seconds': values['connections.window_seconds'],
                        'cooldown_seconds': values['connections.cooldown_seconds']})
    return clients


def _digest(clients):
    return hashlib.sha256(json.dumps(clients, sort_keys=True).encode()).hexdigest()


class ConnectionLimits:
    def __init__(self, db, driver=None):
        self.db, self.driver = db, driver

    def initialize_schema(self):
        with self.db.transaction() as conn:
            create_schema(conn)

    def plan(self, conn):
        """Records a new revision for every changed snapshot; returns what to send."""
        nodes = conn.execute(f'''SELECT n.key,n.protocols_json FROM backend_nodes n
            WHERE n.enabled=1 AND {AGENT_INSTALLED_SQL}
            AND NOT EXISTS (SELECT 1 FROM backend_node_retirements r WHERE r.node_key=n.key)
            AND NOT EXISTS (SELECT 1 FROM backend_node_cleanup c WHERE c.node_key=n.key)
            ORDER BY n.key''').fetchall()
        sends = []
        for node in nodes:
            clients = node_snapshot(conn, node['key']) if 'xray' in json.loads(node['protocols_json']) else []
            digest = _digest(clients)
            row = conn.execute('SELECT * FROM backend_node_admission WHERE node_key=?', (node['key'],)).fetchone()
            if row is None:
                if not clients:
                    continue
                conn.execute('''INSERT INTO backend_node_admission(node_key,revision,digest,status)
                    VALUES (?,1,?,'pending')''', (node['key'], digest))
                revision = 1
            elif row['digest'] != digest:
                revision = row['revision'] + 1
                conn.execute('''UPDATE backend_node_admission SET revision=?,digest=?,status='pending'
                    WHERE node_key=?''', (revision, digest, node['key']))
            else:
                revision = row['revision']
            sends.append((node['key'], revision, clients))
        return sends

    def _record(self, node_key, sent, in_force, status):
        with self.db.transaction() as conn:
            now = datetime.now(timezone.utc).isoformat()
            if in_force is not None and in_force > sent:
                # The agent holds a newer revision, e.g. after a controller
                # restore. Move past it; the next pass resends this snapshot.
                conn.execute('''UPDATE backend_node_admission SET revision=?,status='pending',checked_at=?
                    WHERE node_key=? AND revision=?''', (in_force + 1, now, node_key, sent))
            elif in_force == sent:
                conn.execute('''UPDATE backend_node_admission SET applied_revision=?,status='current',checked_at=?
                    WHERE node_key=? AND revision=?''', (sent, now, node_key, sent))
            else:
                conn.execute('''UPDATE backend_node_admission SET status=?,checked_at=?
                    WHERE node_key=? AND revision=?''', (status, now, node_key, sent))

    def sync(self):
        with self.db.transaction() as conn:
            from .maintenance_gate import active
            if active(conn) or conn.execute(
                    "SELECT 1 FROM backend_backup_jobs WHERE action='restore' AND status IN ('awaiting_executor','running')"
            ).fetchone():
                return []
            sends = self.plan(conn)
        for node_key, revision, clients in sends:
            try:
                in_force = self.driver.apply_admission_policy(node_key, revision, clients)
            except Exception as exc:  # noqa: BLE001 -- the next pass retries
                code = getattr(exc, 'code', lambda: None)()
                outdated = getattr(code, 'name', '') == 'UNIMPLEMENTED'
                logging.getLogger(__name__).warning('Connection limits not delivered: node=%s error=%s',
                    node_key, 'agent_outdated' if outdated else type(exc).__name__)
                self._record(node_key, revision, None, 'outdated' if outdated else 'unavailable')
                continue
            self._record(node_key, revision, in_force, 'unavailable')
        return sends

    def scheduled(self):
        now = datetime.now(timezone.utc)
        with self.db.connect() as conn:
            row = conn.execute('SELECT value FROM backend_system_settings WHERE key=?', (LAST_SYNC,)).fetchone()
        if row and datetime.fromisoformat(json.loads(row['value'])) > now - timedelta(seconds=INTERVAL_SECONDS):
            return False
        with self.db.transaction() as conn:
            conn.execute('''INSERT INTO backend_system_settings(key,value) VALUES (?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value''', (LAST_SYNC, json.dumps(now.isoformat())))
        self.sync()
        return True
