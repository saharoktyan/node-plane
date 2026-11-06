"""Keys of signed clients: Workstation through the relay and the remote bot.

A client keeps an Ed25519 key and signs every request with it; the
controller keeps only the public key. A Workstation key belongs to an
account and acts with its roles; a bot key acts as the Telegram adapter.
Nonces seen in the last minutes are kept to refuse a replayed request.
Workstation links its key with a short code a staff member confirms in the
web cabinet; the request lives ten minutes.
"""


def create_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_client_keys (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK(kind IN ('workstation', 'bot')),
        public_key TEXT NOT NULL UNIQUE,
        account_id TEXT REFERENCES backend_accounts(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_used_at TEXT,
        revoked_at TEXT,
        CHECK((kind = 'workstation') = (account_id IS NOT NULL)))''')
    conn.execute('CREATE INDEX IF NOT EXISTS backend_client_keys_account ON backend_client_keys(account_id)')
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_request_nonces (
        key_id TEXT NOT NULL REFERENCES backend_client_keys(id) ON DELETE CASCADE,
        nonce TEXT NOT NULL,
        seen_at TEXT NOT NULL,
        PRIMARY KEY (key_id, nonce))''')
    conn.execute('CREATE INDEX IF NOT EXISTS backend_request_nonces_seen ON backend_request_nonces(seen_at)')
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_workstation_pairings (
        id TEXT PRIMARY KEY,
        code TEXT NOT NULL UNIQUE,
        public_key TEXT NOT NULL,
        name TEXT NOT NULL,
        source TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'rejected')),
        account_id TEXT REFERENCES backend_accounts(id) ON DELETE CASCADE,
        key_id TEXT REFERENCES backend_client_keys(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL)''')
    conn.execute('CREATE INDEX IF NOT EXISTS backend_workstation_pairings_source ON backend_workstation_pairings(source, created_at)')


def upgrade(conn):
    create_schema(conn)
