"""Access requests name the service they ask for.

Every request so far asks for VPN access; other services (such as a password
manager) get their own requests later without changing the model.
"""

SERVICES = frozenset({'vpn'})


def add_columns(conn):
    if getattr(conn, 'backend_name', '') == 'postgres':
        conn.execute("ALTER TABLE backend_access_requests ADD COLUMN IF NOT EXISTS service TEXT NOT NULL DEFAULT 'vpn'")
        return
    existing = {row['name'] for row in conn.execute('PRAGMA table_info(backend_access_requests)').fetchall()}
    if 'service' not in existing:
        conn.execute("ALTER TABLE backend_access_requests ADD COLUMN service TEXT NOT NULL DEFAULT 'vpn'")


def upgrade(conn):
    add_columns(conn)
