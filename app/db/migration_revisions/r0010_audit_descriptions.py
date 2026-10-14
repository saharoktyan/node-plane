"""Readable, durable descriptions alongside secret-free API audit actions."""


def upgrade(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS backend_workstation_audit_details (
        event_id TEXT PRIMARY KEY REFERENCES backend_workstation_audit(id) ON DELETE CASCADE,
        title TEXT NOT NULL)""")
