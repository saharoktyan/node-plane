"""Persist complete node installation without replaying child operations."""

DDL = (
    '''CREATE TABLE IF NOT EXISTS backend_node_bootstraps (
        id TEXT PRIMARY KEY, node_key TEXT NOT NULL, actor_id TEXT NOT NULL,
        command_key TEXT NOT NULL, revision INTEGER NOT NULL,
        phase TEXT NOT NULL, status TEXT NOT NULL, child_id TEXT,
        child_kind TEXT, error_code TEXT, UNIQUE(actor_id, command_key))''',
    '''CREATE INDEX IF NOT EXISTS backend_node_bootstraps_work
        ON backend_node_bootstraps(status, node_key)''',
)


def upgrade(conn):
    for sql in DDL:
        conn.execute(sql)
