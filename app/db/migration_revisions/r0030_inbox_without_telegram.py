"""Announcement deliveries are Inbox entries; Telegram is only one way to show them.

A delivery without a Telegram chat stays in the Inbox, so its chat may be empty.
"""


def upgrade(conn):
    if getattr(conn, 'backend_name', '') == 'postgres':
        conn.execute('ALTER TABLE backend_announcement_deliveries ALTER COLUMN telegram_subject DROP NOT NULL')
