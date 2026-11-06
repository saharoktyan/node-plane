"""Telegram sign-in to the web cabinet and moving a Telegram between accounts.

`backend_telegram_auth_used` remembers the signed Telegram data already
accepted (Login Widget or Mini App `initData`), so each is used once.
`backend_telegram_links` holds a link in progress: a Telegram proven in the
cabinet, or a code the bot gave out, waiting for the cabinet account to see
what it changes and confirm.
"""


def create_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_telegram_auth_used (
        hash TEXT PRIMARY KEY,
        expires_at TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_telegram_links (
        id TEXT PRIMARY KEY,
        code_hash TEXT UNIQUE,
        account_id TEXT REFERENCES backend_accounts(id) ON DELETE CASCADE,
        subject TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL)''')


def upgrade(conn):
    create_schema(conn)
