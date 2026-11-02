"""Generalize system notifications to typed service events with explicit deduplication."""


def upgrade(conn):
    conn.execute('ALTER TABLE backend_system_notifications ALTER COLUMN expires_at DROP NOT NULL')
    conn.execute('ALTER TABLE backend_system_notifications ADD COLUMN IF NOT EXISTS dedup_key TEXT')
    conn.execute('ALTER TABLE backend_system_notifications ADD COLUMN IF NOT EXISTS device_id TEXT')
    conn.execute("ALTER TABLE backend_system_notifications ADD COLUMN IF NOT EXISTS payload_json TEXT NOT NULL DEFAULT '{}'")
    conn.execute("""UPDATE backend_system_notifications
        SET dedup_key = kind || ':' || profile_id || ':' || expires_at
        WHERE dedup_key IS NULL AND expires_at IS NOT NULL""")
    conn.execute('''CREATE UNIQUE INDEX IF NOT EXISTS backend_system_notifications_dedup
        ON backend_system_notifications(dedup_key)''')
