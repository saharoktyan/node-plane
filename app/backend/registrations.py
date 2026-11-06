"""Open registration (see r0044).

Off by default: invites are the main way in. When it is on, anyone who reaches
the web cabinet can make an account with a login, and a password or a
passkey added right after. The account gets no access and no automatic
request; it asks for a service under "Access to services". To slow a flood
when the domain leaks, each registration solves a proof-of-work puzzle (no
third-party CAPTCHA), an address registers a few accounts a day, and
accounts without access or requests stay out of the main account list.
Optionally, registered accounts left empty are removed after some days
without a sign-in.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import secrets
from uuid import uuid4

from . import logins, sessions
from .authorization import AccessDenied, require_permission

SETTING = 'registration'
DEFAULTS = {'open': False, 'cleanup_days': None}
CLEANUP_DAYS = (7, 365)
# Leading zero bits of sha256("<challenge>:<nonce>"): about 260 thousand hashes.
POW_BITS = 18
CHALLENGE_TTL = timedelta(minutes=10)
PENDING_CHALLENGES = 1000
PER_SOURCE = 5
PER_SOURCE_WINDOW = timedelta(days=1)

# Accounts with no service profile, no pending request and no staff role.
WITHOUT_ACCESS_SQL = '''a.role = 'member' AND NOT EXISTS (
        SELECT 1 FROM backend_service_profiles s WHERE s.account_id = a.id AND NOT EXISTS (
            SELECT 1 FROM backend_profile_deletions pd WHERE pd.profile_id = s.id))
    AND NOT EXISTS (SELECT 1 FROM backend_access_requests r WHERE r.account_id = a.id AND r.status = 'pending')'''


def _now():
    return datetime.now(timezone.utc)


def read(conn):
    row = conn.execute('SELECT value FROM backend_system_settings WHERE key=?', (SETTING,)).fetchone()
    return {**DEFAULTS, **(json.loads(row['value']) if row else {})}


def settings(db, actor):
    require_permission(actor, 'settings.manage')
    with db.connect() as conn:
        return read(conn)


def update(db, actor, *, open=None, cleanup_days=...):
    require_permission(actor, 'settings.manage')
    if open is not None and type(open) is not bool:
        raise AccessDenied('invalid_input', 422)
    if cleanup_days is not ... and cleanup_days is not None and (
            type(cleanup_days) is not int or not CLEANUP_DAYS[0] <= cleanup_days <= CLEANUP_DAYS[1]):
        raise AccessDenied('invalid_input', 422)
    with db.transaction() as conn:
        value = read(conn)
        if open is not None:
            value['open'] = open
        if cleanup_days is not ...:
            value['cleanup_days'] = cleanup_days
        conn.execute('''INSERT INTO backend_system_settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value''', (SETTING, json.dumps(value, sort_keys=True)))
        return value


def solved(challenge, nonce, bits):
    if not isinstance(nonce, str) or not 0 < len(nonce) <= 64:
        return False
    digest = int.from_bytes(hashlib.sha256(f'{challenge}:{nonce}'.encode()).digest(), 'big')
    return digest >> (256 - bits) == 0


def _require_open(conn):
    if not read(conn)['open']:
        raise AccessDenied('registration_closed', 403)


def challenge(db, *, source, now=None):
    """A puzzle to solve before registering: find a nonce whose hash with it
    starts with `bits` zero bits."""
    now = now or _now()
    with db.transaction() as conn:
        _require_open(conn)
        sessions.limited(conn, (sessions.source_scope(source),), now)
        conn.execute('DELETE FROM backend_registration_challenges WHERE expires_at<=?', (now.isoformat(),))
        if conn.execute('SELECT COUNT(*) AS count FROM backend_registration_challenges').fetchone()['count'] \
                >= PENDING_CHALLENGES:
            raise AccessDenied('sign_in_rate_limited', 429)
        challenge_id, value = uuid4().hex, secrets.token_hex(16)
        expires = now + CHALLENGE_TTL
        conn.execute('INSERT INTO backend_registration_challenges(id, challenge, bits, expires_at) VALUES (?, ?, ?, ?)',
                     (challenge_id, value, POW_BITS, expires.isoformat()))
    return {'challenge_id': challenge_id, 'challenge': value, 'bits': POW_BITS, 'expires_at': expires.isoformat()}


def register(db, challenge_id, nonce, *, login, password=None, device=None, source, now=None):
    """Makes an account without access and signs it in."""
    now = now or _now()
    scopes = (sessions.source_scope(source),)
    with db.transaction() as conn:
        _require_open(conn)
        sessions.limited(conn, scopes, now)
        row = conn.execute('SELECT * FROM backend_registration_challenges WHERE id=?',
                           (challenge_id,)).fetchone() if isinstance(challenge_id, str) else None
        if row is not None:
            conn.execute('DELETE FROM backend_registration_challenges WHERE id=?', (row['id'],))
        valid = row is not None and datetime.fromisoformat(row['expires_at']) > now \
            and solved(row['challenge'], nonce, row['bits'])
    if not valid:
        with db.transaction() as conn:
            sessions.failed(conn, scopes, now)
        raise AccessDenied('registration_challenge_invalid', 400)
    with db.transaction() as conn:
        _require_open(conn)
        recent = conn.execute('SELECT COUNT(*) AS count FROM backend_registrations WHERE source=? AND created_at>?',
                              (source, (now - PER_SOURCE_WINDOW).isoformat())).fetchone()['count']
        if recent >= PER_SOURCE:
            raise AccessDenied('registration_rate_limited', 429)
        checked, _ = logins.checked(login)
        if password is not None:
            sessions.checked_password(password, checked)
        account_id = str(uuid4())
        conn.execute("INSERT INTO backend_accounts(id, status, display_name) VALUES (?, 'approved', ?)",
                     (account_id, checked))
        logins.claim(conn, account_id, checked, 'first', now)
        if password is not None:
            sessions.set_password(conn, account_id, password, now=now)
        conn.execute('INSERT INTO backend_registrations(account_id, source, created_at) VALUES (?, ?, ?)',
                     (account_id, source, now.isoformat()))
        from .accounts import AccountService
        return {'account': AccountService.public(AccountService._read(conn, account_id)),
                'session': sessions.open_session(conn, account_id, 'registration', device, now)}


class RegistrationCleanup:
    """Removes registered accounts left without access after the set days
    without a sign-in. Off unless the days are set."""

    def __init__(self, db):
        self.db = db

    def run_one(self, now=None):
        now = now or _now()
        with self.db.transaction() as conn:
            days = read(conn)['cleanup_days']
            if not days:
                return False
            limit = (now - timedelta(days=days)).isoformat()
            row = conn.execute(f'''SELECT a.id FROM backend_registrations g JOIN backend_accounts a ON a.id = g.account_id
                WHERE {WITHOUT_ACCESS_SQL} AND g.created_at <= ?
                AND NOT EXISTS (SELECT 1 FROM backend_account_deletions x WHERE x.account_id = a.id)
                AND NOT EXISTS (SELECT 1 FROM backend_sessions s WHERE s.account_id = a.id AND s.used_at > ?)
                ORDER BY g.created_at LIMIT 1''', (limit, limit)).fetchone()
            if row is None:
                return False
            from .account_deletions import begin, settle
            begin(conn, row['id'])
            settle(conn)
            return True

    def recover(self):
        pass
