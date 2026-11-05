"""Logins: the name an account signs in with, besides its ID.

Everything refers to the account ID; the login is only a way in, so it can
change. `key` is the login in lower case, which keeps logins unique without
regard to case. The account's current login has no `released_at`; a login it
gave up stays held for it for a while, so nobody else takes it at once.
`kind` says how it was claimed: `first` (the account's own first login),
`change` (the account changed it) or `admin` (an administrator set it).
"""


def create_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_logins (
        key TEXT PRIMARY KEY,
        login TEXT NOT NULL,
        account_id TEXT NOT NULL REFERENCES backend_accounts(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK(kind IN ('first', 'change', 'admin')),
        claimed_at TEXT NOT NULL,
        released_at TEXT)''')
    conn.execute('''CREATE UNIQUE INDEX IF NOT EXISTS backend_logins_current
        ON backend_logins(account_id) WHERE released_at IS NULL''')


def upgrade(conn):
    create_schema(conn)
