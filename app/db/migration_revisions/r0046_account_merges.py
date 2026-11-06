"""Merging two accounts of one person, by request.

The person proves both accounts are theirs: the account to be merged in
hands out a code (`backend_merge_codes`), the main account enters it, and a
request (`backend_merge_requests`) waits for an administrator. On approval
the other account's VPN profile, Telegram and sign-in move to the main one,
which is kept, and the other is deleted. `preview_json` keeps what the
administrator saw and decided, as the record of the merge.
"""


def create_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_merge_codes (
        code_hash TEXT PRIMARY KEY,
        account_id TEXT NOT NULL UNIQUE REFERENCES backend_accounts(id) ON DELETE CASCADE,
        expires_at TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_merge_requests (
        id TEXT PRIMARY KEY,
        main_account_id TEXT NOT NULL REFERENCES backend_accounts(id),
        other_account_id TEXT NOT NULL REFERENCES backend_accounts(id),
        status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'rejected', 'cancelled')),
        keep_profile TEXT CHECK(keep_profile IN ('main', 'other')),
        preview_json TEXT,
        created_at TEXT NOT NULL,
        decided_at TEXT,
        decided_by TEXT)''')
    conn.execute('CREATE INDEX IF NOT EXISTS backend_merge_requests_status ON backend_merge_requests(status, created_at)')


def upgrade(conn):
    create_schema(conn)
