"""Confirming a cabinet session again before a dangerous action.

Restoring a backup, cleaning the installation, recovery, an update, removing
a server, or moving the relay, the cabinet or the bot each ask a session of
the web cabinet to show its password or a passkey once more; the
confirmation then holds for a few minutes. Workstation and the bot sign
their requests with their own keys, carry no session and are not asked;
neither are tokens issued without a session.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import sessions
from .authorization import AccessDenied

TTL = timedelta(minutes=5)


def _now():
    return datetime.now(timezone.utc)


def _session(conn, actor):
    return sessions.current(conn, actor.principal.id)


def _record(conn, session_id, method, now):
    conn.execute('''INSERT INTO backend_session_confirmations(session_id, method, confirmed_at) VALUES (?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET method=excluded.method, confirmed_at=excluded.confirmed_at''',
        (session_id, method, now.isoformat()))


def status(db, actor, now=None):
    """Until when this session is confirmed, and how it can confirm itself."""
    now = now or _now()
    from .passkeys import count
    with db.connect() as conn:
        session_id = _session(conn, actor)
        row = conn.execute('SELECT confirmed_at FROM backend_session_confirmations WHERE session_id=?',
                           (session_id,)).fetchone() if session_id else None
        until = datetime.fromisoformat(row['confirmed_at']) + TTL if row else None
        return {'required': session_id is not None,
                'confirmed_until': until.isoformat() if until and until > now else None,
                'password': sessions.has_password(conn, actor.account.id),
                'passkeys': count(conn, actor.account.id) > 0}


def require(db, actor, now=None):
    """Refuses a session without a fresh confirmation; other callers pass."""
    now = now or _now()
    with db.connect() as conn:
        session_id = _session(conn, actor)
        if session_id is None:
            return
        row = conn.execute('SELECT confirmed_at FROM backend_session_confirmations WHERE session_id=?',
                           (session_id,)).fetchone()
    if row is None or datetime.fromisoformat(row['confirmed_at']) + TTL <= now:
        raise AccessDenied('confirmation_required', 403)


def with_password(db, actor, password, now=None):
    """Confirms the session with the account's password, limited as sign-in is."""
    now = now or _now()
    scopes = ('confirm:' + actor.account.id,)
    with db.transaction() as conn:
        session_id = _session(conn, actor)
        if session_id is None:
            raise AccessDenied('confirmation_not_needed', 409)
        sessions.limited(conn, scopes, now)
        row = conn.execute('SELECT hash FROM backend_passwords WHERE account_id=?', (actor.account.id,)).fetchone()
        valid = row is not None and isinstance(password, str) and sessions._verify(row['hash'], password)
        if valid:
            conn.execute('DELETE FROM backend_login_attempts WHERE scope=?', scopes)
            _record(conn, session_id, 'password', now)
        else:
            sessions.failed(conn, scopes, now)
    if not valid:
        # Outside the transaction, so the counted attempt stays.
        raise AccessDenied('password_incorrect', 403)
    return status(db, actor, now)


def passkey_options(db, actor, now=None):
    """Options for `navigator.credentials.get` with this account's passkeys."""
    from . import passkeys
    with db.connect() as conn:
        if _session(conn, actor) is None:
            raise AccessDenied('confirmation_not_needed', 409)
    return passkeys.confirmation_options(db, actor.account.id, now)


def with_passkey(db, actor, challenge_id, credential, now=None):
    from . import passkeys
    now = now or _now()
    passkeys.confirm(db, actor.account.id, challenge_id, credential, now)
    with db.transaction() as conn:
        session_id = _session(conn, actor)
        if session_id is None:
            raise AccessDenied('confirmation_not_needed', 409)
        _record(conn, session_id, 'passkey', now)
    return status(db, actor, now)
