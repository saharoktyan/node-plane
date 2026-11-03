"""Concurrent VLESS source limits: resolve them per client and keep agents current.

The agent enforces a full per-node snapshot and journals it, so limits work
while the controller is unreachable (VPN plan, section 5). The controller
resolves `connections.*` for every active VLESS client on a node and sends
the snapshot every minute. The agent ignores a snapshot whose revision is
not newer than its own, so resending is harmless and also restores a node
whose journal was lost. Nodes that never had a limit are never contacted.

After each delivery the controller reads the node's eviction events and
per-client source counts (`ObserveSessions`). Only counts are stored, never
addresses. An eviction notifies the device owner at most once per
`NOTIFY_MINUTES`; frequent evictions of one device suggest a shared link and
alert administrators.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from uuid import NAMESPACE_URL, uuid5

from .authorization import require_permission
from .nodes import AGENT_INSTALLED_SQL
from .vpn_policies import effective

INTERVAL_SECONDS = 60
KEYS = ('connections.mode', 'connections.max_sources', 'connections.window_seconds',
        'connections.cooldown_seconds')
LAST_SYNC = 'connection_limits_last_sync'
NOTIFY_MINUTES = 30
# This many evictions (or would-be evictions) of one device within
# ALERT_HOURS raise one administrator alert; the next needs as many new ones.
ALERT_EVENTS = 5
ALERT_HOURS = 24
RETENTION_DAYS = 7
# Observations older than this are not shown as current.
FRESH_MINUTES = 5


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


def device_networks(conn, profile_id, now=None):
    """Active devices of a profile with a limit: networks seen in the last
    `FRESH_MINUTES` on any node, and the effective limit."""
    since = ((now or datetime.now(timezone.utc)) - timedelta(minutes=FRESH_MINUTES)).isoformat()
    result = {}
    for row in conn.execute('''SELECT d.id,
            COALESCE(SUM(CASE WHEN o.observed_at>=? THEN o.active_sources END),0) AS active,
            COALESCE(SUM(CASE WHEN o.observed_at>=? THEN o.blocked_sources END),0) AS blocked
        FROM backend_devices d LEFT JOIN backend_vpn_session_observations o ON o.device_id=d.id
        WHERE d.profile_id=? AND d.status='active' GROUP BY d.id''', (since, since, profile_id)).fetchall():
        values = effective(conn, device_id=row['id'], keys=KEYS[:2])
        if values['connections.mode'] != 'off':
            result[row['id']] = {'active': row['active'], 'blocked': row['blocked'],
                                 'limit': values['connections.max_sources']}
    return result


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

    def _store(self, conn, node_key, observed, now):
        cursor = conn.execute('SELECT events_boot,events_sequence FROM backend_node_admission WHERE node_key=?',
                              (node_key,)).fetchone()
        if cursor is None:
            return
        node = conn.execute('SELECT key,title FROM backend_nodes WHERE key=?', (node_key,)).fetchone()
        devices = {}
        for email in {item['email'] for item in observed['sessions'] + observed['events']}:
            row = conn.execute('''SELECT d.id,d.display_name,d.profile_id,p.display_name AS profile_name
                FROM backend_device_credentials c JOIN backend_devices d ON d.id=c.device_id
                JOIN backend_profiles p ON p.id=d.profile_id
                WHERE c.protocol='xray' AND c.runtime_name=?''', (email,)).fetchone()
            if row is not None:
                devices[email] = row
        stamp = now.isoformat()
        conn.execute('DELETE FROM backend_vpn_session_observations WHERE node_key=?', (node_key,))
        for item in observed['sessions']:
            if item['email'] in devices:
                conn.execute('''INSERT INTO backend_vpn_session_observations
                    (device_id,node_key,active_sources,blocked_sources,observed_at) VALUES (?,?,?,?,?)''',
                    (devices[item['email']]['id'], node_key, item['active_sources'], item['blocked_sources'], stamp))
        boot = observed['boot_id']
        sequence = cursor['events_sequence'] if boot == cursor['events_boot'] else 0
        touched = set()
        for event in observed['events']:
            sequence = max(sequence, event['sequence'])
            device = devices.get(event['email'])
            if device is None or event['kind'] not in ('evicted', 'would_evict'):
                continue
            occurred = datetime.fromtimestamp(event['occurred_at'], timezone.utc)
            identity = str(uuid5(NAMESPACE_URL, f"node-plane:admission:{node_key}:{boot}:{event['sequence']}"))
            if conn.execute('''INSERT INTO backend_admission_events(id,device_id,node_key,kind,occurred_at)
                    VALUES (?,?,?,?,?) ON CONFLICT(id) DO NOTHING RETURNING id''',
                    (identity, device['id'], node_key, event['kind'], occurred.isoformat())).fetchone() is None:
                continue
            touched.add(device['id'])
            if event['kind'] == 'evicted' and occurred > now - timedelta(minutes=NOTIFY_MINUTES):
                from .service_notifications import queue
                bucket = int(occurred.timestamp()) // (NOTIFY_MINUTES * 60)
                queue(conn, kind='connection_evicted', profile_id=device['profile_id'], device_id=device['id'],
                      dedup_key=f"connection_evicted:{device['id']}:{bucket}", timestamp=now,
                      payload={'device': device['display_name'], 'node': node['title']})
        conn.execute('UPDATE backend_node_admission SET events_boot=?,events_sequence=? WHERE node_key=?',
                     (boot, sequence, node_key))
        for device in (item for item in devices.values() if item['id'] in touched):
            self._alert(conn, node, device, now)
        conn.execute('DELETE FROM backend_admission_events WHERE occurred_at<?',
                     ((now - timedelta(days=RETENTION_DAYS)).isoformat(),))

    @staticmethod
    def _alert(conn, node, device, now):
        rows = conn.execute('''SELECT id,kind FROM backend_admission_events
            WHERE device_id=? AND alerted=0 AND occurred_at>=?''',
            (device['id'], (now - timedelta(hours=ALERT_HOURS)).isoformat())).fetchall()
        if len(rows) < ALERT_EVENTS:
            return
        from .alerts import AlertService
        AlertService._event(conn, node, 'link_sharing', {
            'profile': device['profile_name'], 'device': device['display_name'], 'events': len(rows),
            'hours': ALERT_HOURS, 'observe': all(row['kind'] == 'would_evict' for row in rows)},
            False, now.isoformat())
        conn.execute(f'''UPDATE backend_admission_events SET alerted=1
            WHERE id IN ({','.join('?' * len(rows))})''', tuple(row['id'] for row in rows))

    def observe(self, node_key):
        with self.db.connect() as conn:
            cursor = conn.execute('SELECT events_boot,events_sequence FROM backend_node_admission WHERE node_key=?',
                                  (node_key,)).fetchone()
        if cursor is None:
            return None
        observed = self.driver.observe_sessions(node_key, cursor['events_boot'], cursor['events_sequence'])
        with self.db.transaction() as conn:
            self._store(conn, node_key, observed, datetime.now(timezone.utc))
        return observed

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
            try:
                self.observe(node_key)
            except Exception as exc:  # noqa: BLE001 -- counts and events wait for the next pass
                logging.getLogger(__name__).warning('Connection observations not read: node=%s error=%s',
                    node_key, type(exc).__name__)
        return sends

    def delivery(self, actor):
        """Per-node delivery of the limits, for nodes that ever had one."""
        require_permission(actor, 'settings.manage')
        with self.db.connect() as conn:
            rows = conn.execute('''SELECT a.node_key,n.title,n.flag,a.status,a.revision,a.applied_revision,a.checked_at
                FROM backend_node_admission a JOIN backend_nodes n ON n.key=a.node_key
                ORDER BY n.title,a.node_key''').fetchall()
        return {'items': [dict(row) for row in rows]}

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
