"""One Xray client per device; the profile-wide client becomes one device's client.

The adopted client keeps the profile UUID and the profile runtime name as
email, so issued links keep working and nodes see no change. Frozen copy of
backend.device_credentials.adopt; that module also converts older backups.
"""
from datetime import datetime, timezone
import json
from uuid import NAMESPACE_URL, uuid5


def upgrade(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_device_credentials (
        device_id TEXT NOT NULL REFERENCES backend_devices(id) ON DELETE CASCADE,
        profile_id TEXT NOT NULL REFERENCES backend_profiles(id) ON DELETE CASCADE,
        protocol TEXT NOT NULL CHECK(protocol IN ('xray')),
        runtime_name TEXT NOT NULL, credential_json TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
        PRIMARY KEY(device_id, protocol), UNIQUE(protocol, runtime_name))''')
    conn.execute('CREATE INDEX IF NOT EXISTS backend_device_credentials_profile ON backend_device_credentials(profile_id)')
    conn.execute("""ALTER TABLE backend_traffic_peers ADD COLUMN IF NOT EXISTS protocol TEXT NOT NULL DEFAULT 'awg'
        CHECK(protocol IN ('awg','xray'))""")
    conn.execute('ALTER TABLE backend_traffic_peers DROP CONSTRAINT IF EXISTS backend_traffic_peers_pkey')
    conn.execute('ALTER TABLE backend_traffic_peers ADD PRIMARY KEY (profile_id,node_key,protocol,device_id)')

    now = datetime.now(timezone.utc).isoformat()
    xray_grants = {row['profile_id'] for row in conn.execute(
        "SELECT profile_id FROM backend_grants WHERE protocol='xray'").fetchall()}
    profiles = conn.execute('''SELECT p.*, i.xray_uuid FROM backend_profiles p
        JOIN backend_profile_identities i ON i.profile_id=p.id
        WHERE EXISTS (SELECT 1 FROM backend_operation_tasks t JOIN backend_operations o ON o.id=t.operation_id
            WHERE o.profile_id=p.id AND t.protocol='xray' AND t.device_id='')
        AND NOT EXISTS (SELECT 1 FROM backend_device_credentials c WHERE c.profile_id=p.id AND c.protocol='xray')
        ORDER BY p.id''').fetchall()
    for profile in profiles:
        own = conn.execute('SELECT * FROM backend_devices WHERE profile_id=? ORDER BY created_at,id',
                           (profile['id'],)).fetchall()
        active = [d for d in own if d['status'] == 'active']
        device = (next((d for d in active if d['runtime_name'] == profile['runtime_name']), None)
                  or (active[0] if active else None))
        created = device is None and (not own or profile['id'] in xray_grants)
        if device is None and not own:
            device = {'id': str(uuid5(NAMESPACE_URL, 'node-plane:default-device:' + profile['id'])),
                      'display_name': 'Device 1', 'runtime_name': profile['runtime_name'],
                      'created_at': profile['created_at'] or now}
        elif device is None and profile['id'] in xray_grants:
            names = {d['display_name'].casefold() for d in own if d['status'] != 'retired'}
            number = next(n for n in range(1, len(names) + 2) if f'device {n}' not in names)
            device = {'id': str(uuid5(NAMESPACE_URL, 'node-plane:xray-device:' + profile['id'])),
                      'display_name': f'Device {number}',
                      'runtime_name': 'd_' + uuid5(NAMESPACE_URL, 'node-plane:xray-device-runtime:' + profile['id']).hex,
                      'created_at': now}
        elif device is None:
            device = own[-1]
        if created:
            conn.execute('''INSERT INTO backend_devices
                (id,profile_id,display_name,runtime_name,status,revision,created_at)
                VALUES (?,?,?,?,'active',1,?)''',
                (device['id'], profile['id'], device['display_name'], device['runtime_name'], device['created_at']))
        conn.execute('''INSERT INTO backend_device_credentials
            (device_id,profile_id,protocol,runtime_name,credential_json,revision,created_at)
            VALUES (?,?,'xray',?,?,1,?)''', (device['id'], profile['id'], profile['runtime_name'],
                json.dumps({'uuid': profile['xray_uuid']}, sort_keys=True), now))
        # Metadata only: command payloads still match the agent journals.
        conn.execute('''UPDATE backend_operation_tasks SET device_id=? WHERE protocol='xray' AND device_id=''
            AND operation_id IN (SELECT id FROM backend_operations WHERE profile_id=?)''', (device['id'], profile['id']))
        conn.execute('''UPDATE backend_config_issuances SET device_id=?,device_revision=(
                SELECT revision FROM backend_devices WHERE id=?)
            WHERE protocol='xray' AND device_id='' AND profile_id=?''', (device['id'], device['id'], profile['id']))
    conn.execute('''INSERT INTO backend_traffic_peers
        (profile_id,node_key,protocol,device_id,epoch,identity,last_uplink,last_downlink,last_sample_at,status)
        SELECT u.profile_id,u.node_key,'xray',c.device_id,u.epoch,u.identity,u.last_uplink,u.last_downlink,u.last_sample_at,u.status
        FROM backend_traffic_usage u JOIN backend_profiles p ON p.id=u.profile_id
        JOIN backend_device_credentials c ON c.profile_id=p.id AND c.protocol='xray' AND c.runtime_name=p.runtime_name
        WHERE u.protocol='xray'
        ON CONFLICT(profile_id,node_key,protocol,device_id) DO NOTHING''')
