"""AWG peers replaced by a key reissue, kept until every node removed them.

A reissue gives the device a new AWG peer name; the node generates new keys
for it. The old name is revoked like a deleted device's peer, under its own
task slot. Controllers that do not know this table would leave the old peer
and its leaked key working, so they must not read this database.
"""


def upgrade(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_retired_peers (
        slot_id TEXT PRIMARY KEY,
        device_id TEXT NOT NULL REFERENCES backend_devices(id) ON DELETE CASCADE,
        profile_id TEXT NOT NULL REFERENCES backend_profiles(id) ON DELETE CASCADE,
        protocol TEXT NOT NULL CHECK(protocol IN ('awg')),
        runtime_name TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL)''')
    conn.execute('CREATE INDEX IF NOT EXISTS backend_retired_peers_profile ON backend_retired_peers(profile_id)')
