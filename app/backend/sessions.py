"""Passwords and sessions (see r0041).

A session is one signed-in device. Signing in gives a short access token and
a refresh token; each refresh replaces both, and a refresh token used a second
time ends the session, since only a copy of it could be used again. Failed
sign-ins are limited per login and per source, and the reply never says
whether the login exists.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
import secrets
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from .authorization import AccessDenied, PrincipalKind

ACCESS_TTL = timedelta(minutes=15)
# A session ends after this long without a refresh.
IDLE_TTL = timedelta(days=30)
WINDOW = timedelta(minutes=15)
LOGIN_FAILURES = 5
SOURCE_FAILURES = 30
PASSWORD_LENGTH = (10, 256)
DEVICE_LENGTH = 128
REFRESH = re.compile(r'nps_([a-f0-9]{32})\.([A-Za-z0-9_-]{43})\Z')

_hasher = PasswordHasher()
# Verified when the login has no password, so the reply takes as long.
_DUMMY = _hasher.hash(secrets.token_urlsafe(16))


def _now():
    return datetime.now(timezone.utc)


def _digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def checked_password(password, login=None):
    if not isinstance(password, str) or not PASSWORD_LENGTH[0] <= len(password) <= PASSWORD_LENGTH[1]:
        raise AccessDenied('password_invalid', 422)
    if login and password.lower() == login.lower():
        raise AccessDenied('password_invalid', 422)
    return password


def has_password(conn, account_id):
    return conn.execute('SELECT 1 FROM backend_passwords WHERE account_id=?', (account_id,)).fetchone() is not None


def _verify(stored, password):
    try:
        return _hasher.verify(stored, password)
    except (VerificationError, InvalidHashError):
        return False


def set_password(conn, account_id, password, *, current=None, keep_session=None, now=None):
    """Sets or changes the account's password; a change needs the current one
    and ends the account's other sessions."""
    from .logins import current as current_login
    checked_password(password, current_login(conn, account_id))
    row = conn.execute('SELECT hash FROM backend_passwords WHERE account_id=?', (account_id,)).fetchone()
    if row is not None and not (isinstance(current, str) and _verify(row['hash'], current)):
        raise AccessDenied('password_incorrect', 403)
    stamp = (now or _now()).isoformat()
    conn.execute('''INSERT INTO backend_passwords(account_id, hash, set_at) VALUES (?, ?, ?)
        ON CONFLICT(account_id) DO UPDATE SET hash=excluded.hash, set_at=excluded.set_at''',
        (account_id, _hasher.hash(password), stamp))
    if row is not None:
        revoke_all(conn, account_id, keep=keep_session, now=now)


def _failures(conn, scope, now):
    return conn.execute('SELECT COUNT(*) AS count FROM backend_login_attempts WHERE scope=? AND attempted_at>?',
                        (scope, (now - WINDOW).isoformat())).fetchone()['count']


def _issue(conn, account_id, session_id, now):
    """An access token of the session, like an issued account credential."""
    from .credentials import PERMISSIONS
    token_id = uuid4().hex
    token = f'np_{token_id}.{secrets.token_urlsafe(32)}'
    expires = now + ACCESS_TTL
    conn.execute('''INSERT INTO backend_credentials
        (id, secret_hash, kind, account_id, scopes_json, created_at, expires_at, session_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (token_id, _digest(token), PrincipalKind.ACCOUNT.value, account_id, json.dumps(sorted(PERMISSIONS)),
         now.isoformat(), expires.isoformat(), session_id))
    return token, expires


def _tokens(conn, session_id, account_id, now):
    access, access_expires = _issue(conn, account_id, session_id, now)
    refresh = f'nps_{session_id}.{secrets.token_urlsafe(32)}'
    expires = now + IDLE_TTL
    return {'session_id': session_id, 'access_token': access, 'access_expires_at': access_expires.isoformat(),
            'refresh_token': refresh, 'refresh_expires_at': expires.isoformat()}, _digest(refresh), expires


def sign_in(db, login, password, *, device, source, now=None):
    """Tokens of a new session, or `sign_in_failed` for any wrong pair."""
    now = now or _now()
    key = login.lower() if isinstance(login, str) and len(login) <= 32 else ''
    device = (device or '').strip()[:DEVICE_LENGTH] or 'Unknown device'
    scopes = ('login:' + key, 'source:' + (source or 'unknown'))
    with db.transaction() as conn:
        if _failures(conn, scopes[0], now) >= LOGIN_FAILURES or _failures(conn, scopes[1], now) >= SOURCE_FAILURES:
            raise AccessDenied('sign_in_rate_limited', 429)
        row = conn.execute('''SELECT a.id, p.hash FROM backend_logins l
            JOIN backend_accounts a ON a.id = l.account_id
            JOIN backend_passwords p ON p.account_id = a.id
            WHERE l.key = ? AND l.released_at IS NULL AND a.status = 'approved'
            AND NOT EXISTS (SELECT 1 FROM backend_account_deletions d WHERE d.account_id = a.id)''',
            (key,)).fetchone() if key else None
        valid = _verify(row['hash'] if row else _DUMMY, password if isinstance(password, str) else '')
        if not (row and valid):
            for scope in scopes:
                conn.execute('INSERT INTO backend_login_attempts(scope, attempted_at) VALUES (?, ?)',
                             (scope, now.isoformat()))
            # Older attempts no longer count.
            conn.execute('DELETE FROM backend_login_attempts WHERE attempted_at<=?', ((now - WINDOW).isoformat(),))
            failed = True
        else:
            failed = False
            if _hasher.check_needs_rehash(row['hash']):
                conn.execute('UPDATE backend_passwords SET hash=? WHERE account_id=?',
                             (_hasher.hash(password), row['id']))
            conn.execute('DELETE FROM backend_login_attempts WHERE scope=?', (scopes[0],))
            session_id = uuid4().hex
            result, refresh_hash, expires = _tokens(conn, session_id, row['id'], now)
            conn.execute('''INSERT INTO backend_sessions(id, account_id, method, device, refresh_hash,
                created_at, used_at, expires_at) VALUES (?, ?, 'password', ?, ?, ?, ?, ?)''',
                (session_id, row['id'], device, refresh_hash, now.isoformat(), now.isoformat(), expires.isoformat()))
    if failed:
        # Outside the transaction, so the counted attempt stays.
        raise AccessDenied('sign_in_failed', 401)
    return result


def refresh(db, token, now=None):
    """New tokens for the session; a replayed refresh token ends it."""
    now = now or _now()
    match = REFRESH.fullmatch(token) if isinstance(token, str) else None
    if not match:
        raise AccessDenied('invalid_credentials', 401)
    digest = _digest(token)
    replayed = False
    with db.transaction() as conn:
        row = conn.execute('SELECT * FROM backend_sessions WHERE id=?', (match[1],)).fetchone()
        if row is None or row['revoked_at'] is not None or datetime.fromisoformat(row['expires_at']) <= now:
            raise AccessDenied('invalid_credentials', 401)
        if row['previous_refresh_hash'] and hmac.compare_digest(row['previous_refresh_hash'], digest):
            _revoke(conn, row['id'], now)
            replayed = True
        elif not hmac.compare_digest(row['refresh_hash'], digest):
            raise AccessDenied('invalid_credentials', 401)
        else:
            # Expired access tokens of the session are of no use any more.
            conn.execute('DELETE FROM backend_credentials WHERE session_id=? AND expires_at<=?',
                         (row['id'], now.isoformat()))
            result, refresh_hash, expires = _tokens(conn, row['id'], row['account_id'], now)
            conn.execute('''UPDATE backend_sessions SET previous_refresh_hash=refresh_hash, refresh_hash=?,
                used_at=?, expires_at=? WHERE id=?''', (refresh_hash, now.isoformat(), expires.isoformat(), row['id']))
    if replayed:
        raise AccessDenied('session_revoked', 401)
    return result


def _revoke(conn, session_id, now):
    stamp = now.isoformat()
    conn.execute('UPDATE backend_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL', (stamp, session_id))
    conn.execute('UPDATE backend_credentials SET revoked_at=? WHERE session_id=? AND revoked_at IS NULL',
                 (stamp, session_id))


def revoke(conn, account_id, session_id, now=None):
    row = conn.execute('SELECT 1 FROM backend_sessions WHERE id=? AND account_id=? AND revoked_at IS NULL',
                       (session_id, account_id)).fetchone()
    if row is None:
        raise AccessDenied('resource_not_found', 404)
    _revoke(conn, session_id, now or _now())


def revoke_all(conn, account_id, *, keep=None, now=None):
    now = now or _now()
    for row in conn.execute('SELECT id FROM backend_sessions WHERE account_id=? AND revoked_at IS NULL',
                            (account_id,)).fetchall():
        if row['id'] != keep:
            _revoke(conn, row['id'], now)


def current(conn, credential_id):
    """The session an access token belongs to, if any."""
    row = conn.execute('SELECT session_id FROM backend_credentials WHERE id=?', (credential_id,)).fetchone()
    return row['session_id'] if row else None


def listed(conn, account_id, current_session=None, now=None):
    now = (now or _now()).isoformat()
    rows = conn.execute('''SELECT id, method, device, created_at, used_at, expires_at FROM backend_sessions
        WHERE account_id=? AND revoked_at IS NULL AND expires_at>? ORDER BY used_at DESC''',
        (account_id, now)).fetchall()
    return [{**dict(row), 'current': row['id'] == current_session} for row in rows]
