"""Passwords and sessions: signing in without an issued token.

`backend_passwords` keeps an argon2id hash per account. A session is one
signed-in device: it holds the digest of its current refresh token and of the
one before it, so a refresh token used twice (stolen and replayed) ends the
session. Access tokens are ordinary credentials that name their session and
die with it. `backend_login_attempts` counts failed sign-ins per login and
per source for the limits.
"""


def create_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_passwords (
        account_id TEXT PRIMARY KEY REFERENCES backend_accounts(id) ON DELETE CASCADE,
        hash TEXT NOT NULL,
        set_at TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_sessions (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES backend_accounts(id) ON DELETE CASCADE,
        method TEXT NOT NULL CHECK(method IN ('password', 'passkey')),
        device TEXT NOT NULL,
        refresh_hash TEXT NOT NULL,
        previous_refresh_hash TEXT,
        created_at TEXT NOT NULL,
        used_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        revoked_at TEXT)''')
    conn.execute('CREATE INDEX IF NOT EXISTS backend_sessions_account ON backend_sessions(account_id)')
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_login_attempts (
        scope TEXT NOT NULL,
        attempted_at TEXT NOT NULL)''')
    conn.execute('CREATE INDEX IF NOT EXISTS backend_login_attempts_scope ON backend_login_attempts(scope, attempted_at)')


def credentials_schema(conn):
    """The session column of credentials, created with the credentials table."""
    if getattr(conn, 'backend_name', '') == 'postgres':
        conn.execute('ALTER TABLE backend_credentials ADD COLUMN IF NOT EXISTS session_id TEXT')
    elif 'session_id' not in {row['name'] for row in conn.execute('PRAGMA table_info(backend_credentials)').fetchall()}:
        conn.execute('ALTER TABLE backend_credentials ADD COLUMN session_id TEXT')


def upgrade(conn):
    create_schema(conn)
    credentials_schema(conn)
