"""Owned VPN policy values keyed by subject and optional node/protocol scope."""


def upgrade(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS backend_vpn_policies (
        id TEXT PRIMARY KEY,
        subject_kind TEXT NOT NULL CHECK(subject_kind IN ('installation','profile','device')),
        profile_id TEXT REFERENCES backend_profiles(id) ON DELETE CASCADE,
        device_id TEXT REFERENCES backend_devices(id) ON DELETE CASCADE,
        node_key TEXT REFERENCES backend_nodes(key) ON DELETE CASCADE,
        protocol TEXT CHECK(protocol IS NULL OR protocol IN ('awg','xray')),
        key TEXT NOT NULL, value_json TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL,
        CHECK((subject_kind='installation' AND profile_id IS NULL AND device_id IS NULL)
            OR (subject_kind='profile' AND profile_id IS NOT NULL AND device_id IS NULL)
            OR (subject_kind='device' AND device_id IS NOT NULL AND profile_id IS NULL)),
        CHECK(protocol IS NULL OR node_key IS NOT NULL))""")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS backend_vpn_policies_target ON backend_vpn_policies(
        subject_kind, COALESCE(profile_id,''), COALESCE(device_id,''),
        COALESCE(node_key,''), COALESCE(protocol,''), key)""")
    conn.execute('CREATE INDEX IF NOT EXISTS backend_vpn_policies_profile ON backend_vpn_policies(profile_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS backend_vpn_policies_device ON backend_vpn_policies(device_id)')
    conn.execute("""CREATE TABLE IF NOT EXISTS backend_vpn_policy_commands (
        principal_id TEXT NOT NULL, actor_id TEXT NOT NULL, command_key TEXT NOT NULL,
        fingerprint TEXT NOT NULL, result_json TEXT,
        PRIMARY KEY(principal_id, actor_id, command_key))""")
