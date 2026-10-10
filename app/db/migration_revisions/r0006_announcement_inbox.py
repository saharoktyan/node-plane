"""Per-recipient dismissal state for durable announcement inbox entries."""


def upgrade(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_announcement_user_state (
        delivery_id TEXT PRIMARY KEY REFERENCES backend_announcement_deliveries(id) ON DELETE CASCADE,
        account_id TEXT NOT NULL REFERENCES backend_accounts(id) ON DELETE CASCADE,
        viewed_at TEXT, dismissed_at TEXT)''')
    conn.execute('''CREATE INDEX IF NOT EXISTS backend_announcement_user_state_account
        ON backend_announcement_user_state(account_id,dismissed_at)''')
