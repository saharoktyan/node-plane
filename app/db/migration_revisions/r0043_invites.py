"""Invites: one-time links an administrator gives out.

An invite either registers a new account and applies its template (a VPN
profile, optionally with a tariff) or, naming an existing account, lets that
account set its login and password, such as for its first sign-in to the web
cabinet or after a lost password. Only the token's digest is kept.
"""


def create_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_invites (
        id TEXT PRIMARY KEY,
        token_hash TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL CHECK(kind IN ('new', 'account')),
        account_id TEXT REFERENCES backend_accounts(id) ON DELETE CASCADE,
        vpn INTEGER NOT NULL,
        tariff_id TEXT,
        note TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used_at TEXT,
        used_account_id TEXT,
        revoked_at TEXT)''')
    conn.execute('CREATE INDEX IF NOT EXISTS backend_invites_created ON backend_invites(created_at)')


def upgrade(conn):
    create_schema(conn)
