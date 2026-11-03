"""Service notifications: System events sent through announcements and the Inbox.

Every event kind declares when a queued delivery is still relevant. Each
recipient chooses how service notifications arrive: `on`, `silent` or `off`.
With `off` the event is not sent but still appears in the Inbox.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Callable
from uuid import NAMESPACE_URL, uuid4, uuid5

MODES = ('on', 'silent', 'off')
SETTING = 'service_notifications:'
SYSTEM_ACTOR = 'system:access-expiry'  # Shared by every System event; kept for stored rows.


@dataclass(frozen=True)
class Kind:
    # Relevance check at claim time, after the shared recipient checks.
    current: Callable
    # English text stored with the announcement; clients localize by kind.
    fallback: Callable


def _access_expired_current(conn, system, delivery):
    row = conn.execute('SELECT expires_at FROM backend_profiles WHERE id=?', (system['profile_id'],)).fetchone()
    return bool(row and row['expires_at'] == system['expires_at']
                and datetime.fromisoformat(row['expires_at']) <= datetime.now(timezone.utc))


def _device_active(conn, system, delivery):
    return bool(conn.execute("SELECT 1 FROM backend_devices WHERE id=? AND status='active'",
                             (system['device_id'],)).fetchone())


def _period_open(conn, system, delivery):
    # Dedup keys of quota events end with the period they belong to.
    period_id = system['dedup_key'].rsplit(':', 1)[-1]
    return conn.execute('SELECT 1 FROM backend_quota_periods WHERE id=? AND closed_at IS NULL',
                        (period_id,)).fetchone() is not None


def _quota_lifted(conn, system, delivery):
    from .quotas import blocked
    return _period_open(conn, system, delivery) and not blocked(conn, system['profile_id'])


def _gigabytes(value):
    return f'{value / 10 ** 9:.1f} GB'


KINDS = {
    'access_expired': Kind(_access_expired_current,
                           lambda payload, title: f'Your access to {title} has expired.'),
    'quota_warning': Kind(_period_open, lambda payload, title: (
        f"You have used {payload['percent']}% of your {title} traffic: "
        f"{_gigabytes(payload['used_bytes'])} of {_gigabytes(payload['limit_bytes'])}.")),
    'quota_exhausted': Kind(_period_open, lambda payload, title: (
        f"Your {title} traffic of {_gigabytes(payload['limit_bytes'])} is used up"
        + ('; access is suspended until the next reset.' if payload['suspended'] else '.'))),
    'quota_reset': Kind(_quota_lifted, lambda payload, title: f'Your {title} traffic was reset; access is restored.'),
    'connection_evicted': Kind(_device_active, lambda payload, title: (
        f"Device {payload['device']} connected to {payload['node']} from another network; "
        'the earlier connection was disconnected.')),
}


def service_title(conn):
    row = conn.execute("SELECT value FROM backend_system_settings WHERE key='bot_menu_title'").fetchone()
    return row['value'].strip() if row and row['value'].strip() else 'Node Plane'


def mode(conn, account_id):
    row = conn.execute('SELECT value FROM backend_system_settings WHERE key=?', (SETTING + account_id,)).fetchone()
    value = json.loads(row['value']) if row else 'on'
    return value if value in MODES else 'on'


def set_mode(conn, account_id, value):
    if value not in MODES:
        raise ValueError('invalid service notification mode')
    conn.execute('''INSERT INTO backend_system_settings(key,value) VALUES (?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value''', (SETTING + account_id, json.dumps(value)))


def queue(conn, *, kind, profile_id, dedup_key, timestamp, payload=None, device_id=None, expires_at=None):
    """Admit one event for the profile owner; repeated dedup keys are no-ops."""
    spec = KINDS[kind]
    if conn.execute('SELECT 1 FROM backend_system_notifications WHERE dedup_key=?', (dedup_key,)).fetchone():
        return None
    recipients = conn.execute("""SELECT a.id AS account_id,i.subject FROM backend_profiles p
        JOIN backend_accounts a ON a.id=p.owner_account_id
        JOIN backend_external_identities i ON i.account_id=a.id AND i.provider='telegram'
        WHERE p.id=? AND a.status='approved'
        AND NOT EXISTS (SELECT 1 FROM backend_profile_deletions d WHERE d.profile_id=p.id)
        ORDER BY i.subject""", (profile_id,)).fetchall()
    if not recipients:
        return None
    payload = payload or {}
    title = service_title(conn)
    identity = str(uuid5(NAMESPACE_URL, 'node-plane:service-notification:' + dedup_key))
    conn.execute('INSERT INTO backend_announcements(id,actor_id,command_key,text,created_at) VALUES (?,?,?,?,?)',
                 (identity, SYSTEM_ACTOR, identity, spec.fallback(payload, title), timestamp.isoformat()))
    conn.execute('''INSERT INTO backend_system_notifications
        (announcement_id,kind,profile_id,expires_at,service_title,dedup_key,device_id,payload_json)
        VALUES (?,?,?,?,?,?,?,?)''', (identity, kind, profile_id, expires_at, title, dedup_key, device_id,
                                      json.dumps(payload, sort_keys=True)))
    for recipient in recipients:
        conn.execute('''INSERT INTO backend_announcement_deliveries
            (id,announcement_id,account_id,telegram_subject,status) VALUES (?,?,?,?,'queued')''',
            (str(uuid4()), identity, recipient['account_id'], recipient['subject']))
    return identity


def current(conn, system, delivery):
    """Whether a queued System delivery should still reach this recipient."""
    spec = KINDS.get(system['kind'])
    if spec is None:
        return False
    owner = conn.execute("""SELECT 1 FROM backend_profiles p
        JOIN backend_accounts a ON a.id=p.owner_account_id
        JOIN backend_external_identities i ON i.account_id=a.id AND i.provider='telegram'
        WHERE p.id=? AND a.id=? AND a.status='approved' AND i.subject=?
        AND NOT EXISTS (SELECT 1 FROM backend_profile_deletions d WHERE d.profile_id=p.id)""",
        (system['profile_id'], delivery['account_id'], delivery['telegram_subject'])).fetchone()
    return bool(owner) and spec.current(conn, system, delivery)
