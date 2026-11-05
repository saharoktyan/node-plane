"""Service profiles of accounts.

The VPN profile is the only service today; its record in `backend_profiles`
shares the id of its service profile. Whatever creates a VPN profile records
its service profile in the same transaction.
"""
from datetime import datetime, timezone

from db.migration_revisions.r0036_service_profiles import SERVICES


def create_schema(conn):
    from db.migration_revisions.r0036_service_profiles import upgrade
    upgrade(conn)


def record(conn, profile_id, account_id, service='vpn'):
    if service not in SERVICES:
        raise ValueError('unknown service')
    conn.execute('''INSERT INTO backend_service_profiles(id,account_id,service,created_at)
        VALUES (?,?,?,?)''', (profile_id, account_id, service, datetime.now(timezone.utc).isoformat()))


def services(conn, account_id):
    """The services the account holds a profile of; a VPN profile being
    deleted no longer counts."""
    return sorted({row['service'] for row in conn.execute('''SELECT s.service FROM backend_service_profiles s
        WHERE s.account_id = ? AND NOT EXISTS (
            SELECT 1 FROM backend_profile_deletions d WHERE d.profile_id = s.id)''', (account_id,)).fetchall()})


def rebuild(conn):
    """After a restore: the service profiles of the restored VPN profiles."""
    from db.migration_revisions.r0036_service_profiles import backfill
    backfill(conn)
