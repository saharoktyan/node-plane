"""Append-only identities for admitted recovery actions and their outcomes."""


def upgrade(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_recovery_audit (
        id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, kind TEXT NOT NULL,
        operation_id TEXT NOT NULL, action TEXT NOT NULL, outcome TEXT NOT NULL,
        status TEXT NOT NULL, created_at TEXT NOT NULL)''')
    conn.execute('CREATE INDEX IF NOT EXISTS backend_recovery_audit_time ON backend_recovery_audit(created_at,id)')
