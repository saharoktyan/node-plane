"""Per-device protocol credentials: one Xray client (UUID and email) per device.

Xray tells clients apart only by UUID, so a VLESS device is its own client.
Before devices existed for VLESS, a profile had one client: the profile UUID
with the profile runtime name as email. Adoption hands that client to one
device unchanged, so links issued earlier keep working without a runtime
change. Devices created later get a fresh UUID and their own runtime name.
"""
from datetime import datetime, timezone
import json
from uuid import NAMESPACE_URL, uuid4, uuid5

from .devices import default_device

TABLE_COLUMNS = ('device_id', 'profile_id', 'protocol', 'runtime_name', 'credential_json',
                 'revision', 'created_at')


def create_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_device_credentials (
        device_id TEXT NOT NULL REFERENCES backend_devices(id) ON DELETE CASCADE,
        profile_id TEXT NOT NULL REFERENCES backend_profiles(id) ON DELETE CASCADE,
        protocol TEXT NOT NULL CHECK(protocol IN ('xray')),
        runtime_name TEXT NOT NULL, credential_json TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
        PRIMARY KEY(device_id, protocol), UNIQUE(protocol, runtime_name))''')
    conn.execute('CREATE INDEX IF NOT EXISTS backend_device_credentials_profile ON backend_device_credentials(profile_id)')


def legacy_xray_device(profile_id):
    """Identity of a device created only to keep a legacy VLESS client."""
    return (str(uuid5(NAMESPACE_URL, 'node-plane:xray-device:' + profile_id)),
            'd_' + uuid5(NAMESPACE_URL, 'node-plane:xray-device-runtime:' + profile_id).hex)


def adopt(profiles, devices, identities, grants, credentials, legacy, created_at):
    """Pure adoption over table rows; returns (new devices, new credentials).

    Also converts backups taken before device credentials existed, so it
    reads only rows that a backup carries. `legacy` names the profiles that
    had a profile-wide Xray client. A profile that already has an Xray
    credential has been adopted. The legacy client goes to:
    the active default device; else the oldest active device; else a new
    default device when the profile has no devices; else, when the profile
    still has an Xray grant, a new device; else the latest device, so the
    next operation revokes it like any other deleted device.
    """
    adopted = {row['profile_id'] for row in credentials if row['protocol'] == 'xray'}
    xray_grants = {row['profile_id'] for row in grants if row['protocol'] == 'xray'}
    by_profile = {}
    for device in devices:
        by_profile.setdefault(device['profile_id'], []).append(device)
    profiles = {row['id']: row for row in profiles}
    new_devices, new_credentials = [], []
    for identity in sorted(identities, key=lambda row: row['profile_id']):
        profile = profiles.get(identity['profile_id'])
        if profile is None or profile['id'] in adopted or profile['id'] not in legacy:
            continue
        own = sorted(by_profile.get(profile['id'], []), key=lambda d: (d['created_at'], d['id']))
        active = [d for d in own if d['status'] == 'active']
        device = (next((d for d in active if d['runtime_name'] == profile['runtime_name']), None)
                  or (active[0] if active else None))
        if device is None and not own:
            device = default_device(profile, created_at)
            new_devices.append(device)
        elif device is None and profile['id'] in xray_grants:
            device_id, runtime_name = legacy_xray_device(profile['id'])
            names = {d['display_name'].casefold() for d in own if d['status'] != 'retired'}
            number = next(n for n in range(1, len(names) + 2) if f'device {n}' not in names)
            device = {'id': device_id, 'profile_id': profile['id'], 'display_name': f'Device {number}',
                      'runtime_name': runtime_name, 'status': 'active', 'revision': 1,
                      'created_at': created_at}
            new_devices.append(device)
        elif device is None:
            device = own[-1]
        new_credentials.append({
            'device_id': device['id'], 'profile_id': profile['id'], 'protocol': 'xray',
            'runtime_name': profile['runtime_name'],
            'credential_json': json.dumps({'uuid': identity['xray_uuid']}, sort_keys=True),
            'revision': 1, 'created_at': created_at})
    return new_devices, new_credentials


def adopt_existing(conn):
    """Startup adoption for repository fixtures; PostgreSQL adopts in r0017."""
    rows = {table: [dict(row) for row in conn.execute(f'SELECT * FROM {table}').fetchall()]
            for table in ('backend_profiles', 'backend_devices', 'backend_profile_identities',
                          'backend_grants', 'backend_device_credentials')}
    legacy = {row['profile_id'] for row in conn.execute('''SELECT DISTINCT o.profile_id
        FROM backend_operation_tasks t JOIN backend_operations o ON o.id=t.operation_id
        WHERE t.protocol='xray' AND t.device_id='' ''').fetchall()}
    devices, credentials = adopt(rows['backend_profiles'], rows['backend_devices'],
        rows['backend_profile_identities'], rows['backend_grants'], rows['backend_device_credentials'],
        legacy, datetime.now(timezone.utc).isoformat())
    if getattr(conn, 'backend_name', '') == 'postgres':
        issuances = conn.execute("SELECT to_regclass('backend_config_issuances') AS name").fetchone()['name'] is not None
    else:
        issuances = conn.execute('''SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='backend_config_issuances' ''').fetchone() is not None
    insert(conn, devices, credentials, issuances=issuances)


def insert(conn, devices, credentials, *, issuances=True):
    from .devices import DEVICE_COLUMNS
    for device in devices:
        conn.execute(f'''INSERT INTO backend_devices ({','.join(DEVICE_COLUMNS)})
            VALUES ({','.join('?' * len(DEVICE_COLUMNS))})''', tuple(device[key] for key in DEVICE_COLUMNS))
    for credential in credentials:
        conn.execute(f'''INSERT INTO backend_device_credentials ({','.join(TABLE_COLUMNS)})
            VALUES ({','.join('?' * len(TABLE_COLUMNS))})''', tuple(credential[key] for key in TABLE_COLUMNS))
        # Earlier tasks and issuances named the profile client without a device.
        conn.execute('''UPDATE backend_operation_tasks SET device_id=? WHERE protocol='xray' AND device_id=''
            AND operation_id IN (SELECT id FROM backend_operations WHERE profile_id=?)''',
            (credential['device_id'], credential['profile_id']))
        if issuances:
            conn.execute('''UPDATE backend_config_issuances SET device_id=?,device_revision=(
                    SELECT revision FROM backend_devices WHERE id=?)
                WHERE protocol='xray' AND device_id='' AND profile_id=?''',
                (credential['device_id'], credential['device_id'], credential['profile_id']))


def xray(conn, device_id):
    row = conn.execute("SELECT * FROM backend_device_credentials WHERE device_id=? AND protocol='xray'",
                       (device_id,)).fetchone()
    return None if row is None else {'runtime_name': row['runtime_name'], 'revision': row['revision'],
                                     **json.loads(row['credential_json'])}


def ensure_xray(conn, device):
    """The device's Xray client, created on first use."""
    existing = xray(conn, device['id'])
    if existing is not None:
        return existing
    conn.execute('''INSERT INTO backend_device_credentials
        (device_id,profile_id,protocol,runtime_name,credential_json,revision,created_at)
        VALUES (?,?,'xray',?,?,1,?)''', (device['id'], device['profile_id'], device['runtime_name'],
            json.dumps({'uuid': str(uuid4())}), datetime.now(timezone.utc).isoformat()))
    return xray(conn, device['id'])


def rotate_xray(conn, device):
    """A new UUID for the same client; the old link stops working."""
    ensure_xray(conn, device)
    conn.execute('''UPDATE backend_device_credentials SET credential_json=?,revision=revision+1
        WHERE device_id=? AND protocol='xray' ''', (json.dumps({'uuid': str(uuid4())}), device['id']))
    return xray(conn, device['id'])


def xray_peers(conn, profile_id):
    """Every device that has an Xray client, with that client's identity."""
    rows = conn.execute('''SELECT d.id,d.status,c.runtime_name,c.credential_json FROM backend_devices d
        JOIN backend_device_credentials c ON c.device_id=d.id AND c.protocol='xray'
        WHERE d.profile_id=? ORDER BY d.id''', (profile_id,)).fetchall()
    return [{'id': row['id'], 'status': row['status'], 'runtime_name': row['runtime_name'],
             'uuid': json.loads(row['credential_json'])['uuid']} for row in rows]
