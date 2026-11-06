"""Passkeys: WebAuthn credentials of accounts.

`backend_passkeys` keeps each credential's public key (COSE) and signature
counter, bound to the relying party it was made for, which is the relay's
domain. `backend_webauthn_challenges` holds the challenge of a ceremony in
progress; it is used once and expires within minutes.
"""


def create_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_passkeys (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES backend_accounts(id) ON DELETE CASCADE,
        rp_id TEXT NOT NULL,
        public_key TEXT NOT NULL,
        sign_count INTEGER NOT NULL,
        transports_json TEXT NOT NULL,
        name TEXT NOT NULL,
        aaguid TEXT NOT NULL,
        backed_up INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        used_at TEXT)''')
    conn.execute('CREATE INDEX IF NOT EXISTS backend_passkeys_account ON backend_passkeys(account_id)')
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_webauthn_challenges (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK(kind IN ('register', 'sign_in')),
        account_id TEXT REFERENCES backend_accounts(id) ON DELETE CASCADE,
        rp_id TEXT NOT NULL,
        origin TEXT NOT NULL,
        challenge TEXT NOT NULL,
        expires_at TEXT NOT NULL)''')


def upgrade(conn):
    create_schema(conn)
