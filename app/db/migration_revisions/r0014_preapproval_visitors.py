"""Pre-approval visitor access requests without allocating backend accounts."""


def upgrade(conn):
    conn.execute('ALTER TABLE backend_access_requests ALTER COLUMN account_id DROP NOT NULL')
    conn.execute('''ALTER TABLE backend_access_requests
        ADD COLUMN IF NOT EXISTS telegram_subject TEXT REFERENCES backend_telegram_identity_details(subject)''')
    conn.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_backend_guest_request_key
        ON backend_access_requests(telegram_subject, create_key) WHERE telegram_subject IS NOT NULL''')
    conn.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_backend_pending_guest_request
        ON backend_access_requests(telegram_subject) WHERE status = 'pending' AND account_id IS NULL''')
    conn.execute('''ALTER TABLE backend_access_requests
        ADD CONSTRAINT backend_access_requests_identity_check
        CHECK (account_id IS NOT NULL OR telegram_subject IS NOT NULL)''')
    conn.execute("""CREATE TABLE IF NOT EXISTS backend_telegram_menu_refresh (
        subject TEXT PRIMARY KEY REFERENCES backend_telegram_identity_details(subject),
        revision INTEGER NOT NULL)""")
