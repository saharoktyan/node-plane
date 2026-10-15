"""System event metadata shares the durable announcement transport and Inbox."""


def upgrade(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS backend_system_notifications (
        announcement_id TEXT PRIMARY KEY REFERENCES backend_announcements(id) ON DELETE CASCADE,
        kind TEXT NOT NULL, profile_id TEXT NOT NULL, expires_at TEXT NOT NULL,
        service_title TEXT NOT NULL,
        UNIQUE(kind,profile_id,expires_at))""")
