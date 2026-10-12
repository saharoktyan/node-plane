"""Durable menu invalidations after account role/status changes."""


def upgrade(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS backend_account_menu_refresh (
        account_id TEXT PRIMARY KEY REFERENCES backend_accounts(id) ON DELETE CASCADE,
        revision INTEGER NOT NULL)""")
