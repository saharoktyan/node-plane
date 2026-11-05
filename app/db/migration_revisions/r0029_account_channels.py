"""Account channels: where an account is reached, apart from how it signs in.

Telegram was both the login and the only place notifications went. A channel
is now its own row; existing Telegram logins become Telegram channels.
"""


def create_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_account_channels (
        account_id TEXT NOT NULL REFERENCES backend_accounts(id) ON DELETE CASCADE,
        channel TEXT NOT NULL CHECK(channel IN ('telegram', 'web')),
        address TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY(account_id, channel),
        UNIQUE(channel, address))''')


def backfill(conn):
    """Give every Telegram login its Telegram channel."""
    conn.execute('''INSERT INTO backend_account_channels(account_id, channel, address)
        SELECT i.account_id, 'telegram', MIN(i.subject) FROM backend_external_identities i
        WHERE i.provider = 'telegram' GROUP BY i.account_id
        ON CONFLICT DO NOTHING''')


def upgrade(conn):
    create_schema(conn)
    backfill(conn)
