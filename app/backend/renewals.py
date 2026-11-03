"""Renewal requests: a profile owner asks to extend access; an administrator
approves or rejects.

Approval is the profile command `renew` (backend.profile_commands), so the
new term, the profile revision and its runtime operation commit together
with the decision. The term is added after the remaining time, or from now
when access has expired, like extending a tariff. Without a chosen number of
days the profile's tariff term is used.
"""
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from .authorization import AccessDenied, require_permission

COLUMNS = ('id', 'profile_id', 'account_id', 'status', 'created_at', 'decided_at', 'days', 'expires_at')


def create_schema(conn):
    from db.migration_revisions.r0023_renewal_requests import upgrade
    upgrade(conn)


def public(row):
    return None if row is None else {column: row[column] for column in COLUMNS}


def latest(conn, profile_id):
    return public(conn.execute('''SELECT * FROM backend_renewal_requests WHERE profile_id=?
        ORDER BY created_at DESC, id DESC LIMIT 1''', (profile_id,)).fetchone())


def _key(value):
    try:
        return str(UUID(value))
    except (TypeError, ValueError, AttributeError):
        raise AccessDenied('invalid_idempotency_key', 422) from None


def _notify(conn, kind, request, payload):
    from .service_notifications import queue
    queue(conn, kind=kind, profile_id=request['profile_id'], dedup_key=f"{kind}:{request['id']}",
          timestamp=datetime.now(timezone.utc), payload=payload)


def approve(conn, actor, profile_id, current_expiry, values):
    """Decide a pending request inside the `renew` profile command; returns
    the profile's new expiry."""
    if not isinstance(values, dict) or set(values) - {'request_id', 'days'} or 'request_id' not in values:
        raise AccessDenied('invalid_input', 422)
    request = conn.execute('''UPDATE backend_renewal_requests SET status=status
        WHERE id=? AND profile_id=? RETURNING *''', (str(values['request_id']), profile_id)).fetchone()
    if request is None:
        raise AccessDenied('resource_not_found', 404)
    if request['status'] != 'pending':
        raise AccessDenied('renewal_decided', 409)
    days = values.get('days')
    if days is None:
        tariff = conn.execute('''SELECT t.duration_days FROM backend_profile_tariffs a
            JOIN backend_tariffs t ON t.id=a.tariff_id WHERE a.profile_id=?''', (profile_id,)).fetchone()
        days = tariff['duration_days'] if tariff else None
        if days is None:
            raise AccessDenied('renewal_days_required', 422)
    if type(days) is not int or not 1 <= days <= 3650:
        raise AccessDenied('invalid_input', 422)
    now = datetime.now(timezone.utc)
    start = now
    if current_expiry and datetime.fromisoformat(current_expiry) > now:
        start = datetime.fromisoformat(current_expiry)
    expires_at = (start + timedelta(days=days)).astimezone(timezone.utc).isoformat()
    conn.execute('''UPDATE backend_renewal_requests SET status='approved',decided_at=?,decided_by=?,days=?,expires_at=?
        WHERE id=?''', (now.isoformat(), actor.account.id, days, expires_at, request['id']))
    _notify(conn, 'renewal_approved', request, {'expires_at': expires_at, 'days': days})
    return expires_at


class RenewalRequests:
    def __init__(self, db):
        self.db = db

    def initialize_schema(self):
        with self.db.transaction() as conn:
            create_schema(conn)

    def request(self, actor, profile_id, command_key):
        """The owner's request; a repeated or second request returns the pending one."""
        require_permission(actor, 'profiles.self.read')
        command_key = _key(command_key)
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            replay = conn.execute('SELECT * FROM backend_renewal_requests WHERE account_id=? AND create_key=?',
                                  (actor.account.id, command_key)).fetchone()
            if replay is not None:
                return public(replay)
            # The lock serializes requests of one profile.
            profile = conn.execute('''UPDATE backend_profiles SET desired_revision=desired_revision
                WHERE id=? AND owner_account_id=?
                AND NOT EXISTS (SELECT 1 FROM backend_profile_deletions d WHERE d.profile_id=backend_profiles.id)
                RETURNING expires_at''', (profile_id, actor.account.id)).fetchone()
            if profile is None:
                raise AccessDenied('resource_not_found', 404)
            if profile['expires_at'] is None:
                # Access without a term needs no renewal.
                raise AccessDenied('renewal_not_needed', 409)
            pending = conn.execute("SELECT * FROM backend_renewal_requests WHERE profile_id=? AND status='pending'",
                                   (profile_id,)).fetchone()
            if pending is not None:
                return public(pending)
            row = {'id': str(uuid4()), 'created_at': datetime.now(timezone.utc).isoformat()}
            conn.execute('''INSERT INTO backend_renewal_requests(id,profile_id,account_id,create_key,status,created_at)
                VALUES (?,?,?,?,'pending',?)''', (row['id'], profile_id, actor.account.id, command_key, row['created_at']))
            return public(conn.execute('SELECT * FROM backend_renewal_requests WHERE id=?', (row['id'],)).fetchone())

    def pending(self, actor, offset=0, limit=20):
        require_permission(actor, 'profiles.manage')
        with self.db.connect() as conn:
            total = conn.execute("SELECT COUNT(*) AS count FROM backend_renewal_requests WHERE status='pending'"
                                 ).fetchone()['count']
            rows = conn.execute('''SELECT r.*, p.display_name, p.expires_at AS profile_expires_at,
                p.desired_revision, t.title AS tariff_title, t.duration_days
                FROM backend_renewal_requests r JOIN backend_profiles p ON p.id=r.profile_id
                LEFT JOIN backend_profile_tariffs a ON a.profile_id=p.id
                LEFT JOIN backend_tariffs t ON t.id=a.tariff_id
                WHERE r.status='pending' ORDER BY r.created_at, r.id LIMIT ? OFFSET ?''',
                (limit, offset)).fetchall()
        return {'total': total, 'offset': offset, 'items': [
            {**public(row), 'display_name': row['display_name'], 'profile_expires_at': row['profile_expires_at'],
             'profile_revision': row['desired_revision'],
             'tariff': None if row['tariff_title'] is None else {'title': row['tariff_title'],
                                                                 'duration_days': row['duration_days']}}
            for row in rows]}

    def reject(self, actor, request_id):
        require_permission(actor, 'profiles.manage')
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            request = conn.execute('UPDATE backend_renewal_requests SET status=status WHERE id=? RETURNING *',
                                   (str(request_id),)).fetchone()
            if request is None:
                raise AccessDenied('resource_not_found', 404)
            if request['status'] == 'rejected':
                return public(request)
            if request['status'] != 'pending':
                raise AccessDenied('renewal_decided', 409)
            conn.execute('''UPDATE backend_renewal_requests SET status='rejected',decided_at=?,decided_by=?
                WHERE id=?''', (datetime.now(timezone.utc).isoformat(), actor.account.id, request['id']))
            _notify(conn, 'renewal_rejected', request, {})
            return public(conn.execute('SELECT * FROM backend_renewal_requests WHERE id=?',
                                       (request['id'],)).fetchone())
