"""Speed limits per device: resolve them per node and keep agents current.

The controller resolves `speed.*` for every active Xray client and AWG peer
on a node and sends the node's snapshot every minute (delivery:
backend.node_snapshots). While a profile with `quota.on_exhausted =
throttle` has used up its quota, `quota.throttle_kbit` caps its devices.
The agent journals the snapshot and shapes on its own, so limits keep
working while the controller is unreachable (VPN plan, section 11d). The agent answers with the outcome of its latest pass per protocol;
`unsupported` means the node lacks tc, nft or nsenter.
"""
from datetime import datetime, timedelta, timezone
import json
import logging

from .authorization import require_permission
from .vpn_policies import effective

INTERVAL_SECONDS = 60
KEYS = ('speed.up_kbit', 'speed.down_kbit')
LAST_SYNC = 'speed_limits_last_sync'
TABLE = 'backend_node_shaping'


def create_schema(conn):
    from db.migration_revisions.r0021_speed_limits import upgrade
    upgrade(conn)


def node_snapshot(conn, node_key, protocols, now=None):
    """Limited clients of active devices on this node, both protocols."""
    from .node_snapshots import active_devices
    from .quotas import throttle
    clients, throttled = [], {}
    for protocol in sorted({'awg', 'xray'} & set(protocols)):
        for row in active_devices(conn, node_key, protocol, now):
            values = effective(conn, device_id=row['device_id'], node_key=node_key, protocol=protocol, keys=KEYS)
            up, down = values['speed.up_kbit'] or 0, values['speed.down_kbit'] or 0
            if row['profile_id'] not in throttled:
                throttled[row['profile_id']] = throttle(conn, row['profile_id'])
            if limit := throttled[row['profile_id']]:
                # A used-up quota caps both directions; a lower own limit stays.
                up, down = min(up or limit, limit), min(down or limit, limit)
            if up or down:
                clients.append({'protocol': protocol, 'name': row['name'], 'up_kbit': up, 'down_kbit': down})
    return clients


class SpeedLimits:
    def __init__(self, db, driver=None):
        self.db, self.driver = db, driver

    def initialize_schema(self):
        with self.db.transaction() as conn:
            create_schema(conn)

    @staticmethod
    def plan(conn):
        from .node_snapshots import plan
        return plan(conn, TABLE, lambda conn, node: node_snapshot(
            conn, node['key'], json.loads(node['protocols_json'])))

    def _record(self, node_key, sent, in_force, status, protocols=None):
        from .node_snapshots import record
        with self.db.transaction() as conn:
            extra = {} if protocols is None else {'protocols_json': json.dumps(protocols)}
            record(conn, TABLE, node_key, sent, in_force, status, **extra)

    def sync(self):
        from .node_snapshots import failure_status, paused
        with self.db.transaction() as conn:
            if paused(conn):
                return []
            sends = self.plan(conn)
        for node_key, revision, clients in sends:
            try:
                applied = self.driver.apply_rate_policy(node_key, revision, clients)
            except Exception as exc:  # noqa: BLE001 -- the next pass retries
                status = failure_status(exc)
                logging.getLogger(__name__).warning('Speed limits not delivered: node=%s error=%s',
                    node_key, 'agent_outdated' if status == 'outdated' else type(exc).__name__)
                self._record(node_key, revision, None, status)
                continue
            self._record(node_key, revision, applied['revision'], 'unavailable', applied['protocols'])
        return sends

    def delivery(self, actor):
        """Per-node delivery and the agent's outcome per protocol, for nodes
        that ever had a limit."""
        require_permission(actor, 'settings.manage')
        with self.db.connect() as conn:
            rows = conn.execute(f'''SELECT s.node_key,n.title,n.flag,s.status,s.revision,s.applied_revision,
                s.checked_at,s.protocols_json FROM {TABLE} s JOIN backend_nodes n ON n.key=s.node_key
                ORDER BY n.title,s.node_key''').fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item['protocols'] = json.loads(item.pop('protocols_json'))
            items.append(item)
        return {'items': items}

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
