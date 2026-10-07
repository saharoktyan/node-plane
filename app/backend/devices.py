"""Stable AWG device identities and confirmed retirement."""
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5


DEVICE_COLUMNS = (
    'id', 'profile_id', 'display_name', 'runtime_name', 'status', 'revision',
    'created_at',
)


def default_device(profile, created_at=None):
    """Deterministic adoption also works when converting a pre-device backup."""
    return {
        'id': str(uuid5(NAMESPACE_URL, 'node-plane:default-device:' + profile['id'])),
        'profile_id': profile['id'],
        'display_name': 'Device 1',
        'runtime_name': profile['runtime_name'],
        'status': 'active',
        'revision': 1,
        'created_at': profile['created_at'] or created_at or datetime.now(timezone.utc).isoformat(),
    }


class DeviceRepository:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def create_schema(conn):
        conn.execute('''CREATE TABLE IF NOT EXISTS backend_devices (
            id TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL REFERENCES backend_profiles(id) ON DELETE CASCADE,
            display_name TEXT NOT NULL,
            runtime_name TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK(status IN ('active', 'deleting', 'retired')),
            revision INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS backend_devices_profile ON backend_devices(profile_id, id)')
        conn.execute('''CREATE TABLE IF NOT EXISTS backend_device_commands (
            principal_id TEXT NOT NULL,actor_id TEXT NOT NULL,command_key TEXT NOT NULL,
            fingerprint TEXT NOT NULL,result_json TEXT,
            PRIMARY KEY(principal_id,actor_id,command_key))''')

    @staticmethod
    def ensure_default(conn, profile):
        # Serialize adoption on the parent, including concurrent worker/API
        # startup. Any history prevents accidental resurrection of a device.
        conn.execute('UPDATE backend_profiles SET desired_revision=desired_revision WHERE id=?', (profile['id'],))
        existing = conn.execute('SELECT id FROM backend_devices WHERE profile_id=? ORDER BY id LIMIT 1', (profile['id'],)).fetchone()
        if existing:
            return existing['id']
        device = default_device(profile)
        conn.execute('''INSERT INTO backend_devices
            (id, profile_id, display_name, runtime_name, status, revision, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)''', tuple(device[key] for key in DEVICE_COLUMNS))
        return device['id']

    @classmethod
    def adopt_existing(cls, conn):
        # Historical ensures still identify a remote peer after grants have
        # been removed. Adoption must not issue any new mutation or rotate keys.
        profiles = conn.execute('''SELECT p.* FROM backend_profiles p
            WHERE NOT EXISTS (SELECT 1 FROM backend_devices d WHERE d.profile_id=p.id)
            AND (EXISTS (SELECT 1 FROM backend_grants g WHERE g.profile_id=p.id AND g.protocol='awg')
                OR EXISTS (SELECT 1 FROM backend_operation_tasks t
                    JOIN backend_operations o ON o.id=t.operation_id
                    WHERE o.profile_id=p.id AND t.protocol='awg'))
            ORDER BY p.id''').fetchall()
        for profile in profiles:
            cls.ensure_default(conn, profile)

    @staticmethod
    def select_active(conn, profile_id, device_id=None):
        from .authorization import AccessDenied
        if device_id:
            row = conn.execute('SELECT * FROM backend_devices WHERE id=? AND profile_id=?', (device_id,profile_id)).fetchone()
            if row is None:
                raise AccessDenied('resource_not_found',404)
            if row['status'] != 'active':
                raise AccessDenied('device_unavailable',409)
            return row
        rows = conn.execute("SELECT * FROM backend_devices WHERE profile_id=? AND status='active' ORDER BY id LIMIT 2", (profile_id,)).fetchall()
        if not rows:
            raise AccessDenied('device_unavailable',409)
        if len(rows) != 1:
            raise AccessDenied('device_required',409)
        return rows[0]

    @staticmethod
    def public(row):
        return {key: row[key] for key in ('id', 'profile_id', 'display_name', 'status', 'revision', 'created_at')}

    def list_for_profile(self, profile_id):
        """Internal read; callers must authorize the parent profile first."""
        with self.db.connect() as conn:
            rows = conn.execute('SELECT * FROM backend_devices WHERE profile_id=? ORDER BY created_at,id', (profile_id,)).fetchall()
        return [self.public(row) for row in rows]

    @staticmethod
    def settle_deletions(conn):
        devices = conn.execute("SELECT id,profile_id FROM backend_devices WHERE status='deleting' ORDER BY id").fetchall()
        for device in devices:
            tasks = conn.execute('''SELECT t.node_key,t.action,t.status,r.mode FROM backend_operation_tasks t
                JOIN backend_operations o ON o.id=t.operation_id
                LEFT JOIN backend_node_retirements r ON r.node_key=t.node_key
                WHERE o.profile_id=? AND t.device_id=? ORDER BY o.desired_revision DESC,o.created_at DESC,t.id DESC''',
                (device['profile_id'],device['id'])).fetchall()
            latest = {}
            uncertain = False
            for task in tasks:
                latest.setdefault(task['node_key'],task)
                if task['mode'] != 'verified' and task['status'] in {'awaiting_executor','running','blocked'}:
                    uncertain = True
            if not uncertain and all(t['mode']=='verified' or (t['action'],t['status'])==('delete','succeeded') for t in latest.values()):
                conn.execute("UPDATE backend_devices SET status='retired' WHERE id=? AND status='deleting'",(device['id'],))
