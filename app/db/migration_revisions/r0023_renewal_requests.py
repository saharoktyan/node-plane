"""Renewal requests: a profile owner asks an administrator to extend access.

Workflow state, not backed up; older controllers ignore the table.
"""


def upgrade(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_renewal_requests (
        id TEXT PRIMARY KEY,
        profile_id TEXT NOT NULL REFERENCES backend_profiles(id) ON DELETE CASCADE,
        account_id TEXT NOT NULL REFERENCES backend_accounts(id),
        create_key TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'rejected')),
        created_at TEXT NOT NULL,
        decided_at TEXT,
        decided_by TEXT REFERENCES backend_accounts(id),
        days INTEGER,
        expires_at TEXT,
        UNIQUE(account_id, create_key))''')
    conn.execute('''CREATE UNIQUE INDEX IF NOT EXISTS backend_renewal_requests_pending
        ON backend_renewal_requests(profile_id) WHERE status='pending' ''')
    conn.execute('''CREATE INDEX IF NOT EXISTS backend_renewal_requests_status
        ON backend_renewal_requests(status, created_at)''')
