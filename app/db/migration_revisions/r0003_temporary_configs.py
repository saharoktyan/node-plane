"""Independent temporary identities and secret-free lifecycle history."""

DDL = (
    '''CREATE TABLE IF NOT EXISTS backend_temporary_configs (
        id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, principal_id TEXT NOT NULL,
        command_key TEXT NOT NULL, node_key TEXT NOT NULL, node_revision INTEGER NOT NULL,
        protocol TEXT NOT NULL CHECK(protocol IN ('awg','xray')),
        transport TEXT NOT NULL CHECK(transport IN ('vpn','conf','tcp','xhttp')),
        duration_seconds INTEGER NOT NULL CHECK(duration_seconds IN (43200,86400,259200)),
        runtime_name TEXT NOT NULL UNIQUE, intent_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('queued','issuing','blocked','active',
            'revoking','revoke_blocked','expired','revoked','cancelled')),
        created_at TEXT NOT NULL, expires_at TEXT, revoke_reason TEXT,
        error_code TEXT, UNIQUE(actor_id,command_key))''',
    '''CREATE INDEX IF NOT EXISTS backend_temporary_configs_work
        ON backend_temporary_configs(status,expires_at,id)''',
    '''CREATE TABLE IF NOT EXISTS backend_temporary_config_events (
        id TEXT PRIMARY KEY, config_id TEXT NOT NULL, actor_id TEXT NOT NULL,
        principal_id TEXT NOT NULL, node_key TEXT NOT NULL, protocol TEXT NOT NULL,
        action TEXT NOT NULL, status TEXT NOT NULL, occurred_at TEXT NOT NULL)''',
)


def upgrade(conn):
    for sql in DDL:
        conn.execute(sql)
