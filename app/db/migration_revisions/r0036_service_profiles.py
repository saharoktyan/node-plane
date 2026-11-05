"""Service profiles: what an account holds of each service.

How many profiles of a service an account may hold is the service's policy,
not the schema's; for VPN it is one (`idx_backend_profile_single_owner`).
The VPN profile (`backend_profiles`) is the first kind: it shares its id
with its service profile and keeps the VPN state itself (devices, grants,
term, freezing).
"""

SERVICES = ('vpn',)


def create_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_service_profiles (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES backend_accounts(id),
        service TEXT NOT NULL,
        created_at TEXT NOT NULL)''')
    conn.execute('''CREATE INDEX IF NOT EXISTS idx_backend_service_profiles_account
        ON backend_service_profiles(account_id, service)''')


def backfill(conn):
    """A service profile for every owned VPN profile."""
    from datetime import datetime, timezone
    conn.execute('''INSERT INTO backend_service_profiles(id,account_id,service,created_at)
        SELECT p.id, p.owner_account_id, 'vpn', COALESCE(p.created_at, ?) FROM backend_profiles p
        WHERE p.owner_account_id IS NOT NULL ORDER BY p.id ON CONFLICT DO NOTHING''',
        (datetime.now(timezone.utc).isoformat(),))


def upgrade(conn):
    create_schema(conn)
    backfill(conn)
