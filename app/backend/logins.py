"""Account logins: rules, claiming and holding (see r0040).

A login is 3–32 Latin letters, digits, `_`, `.` or `-`, starting with a
letter or digit, unique without regard to case. An account may change its
own login once in `CHANGE_EVERY`, and a login it gave up stays held for it
for `HOLD`; an administrator may change any login at any time.
"""
import re
from datetime import datetime, timedelta, timezone

from .authorization import AccessDenied

PATTERN = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{2,31}\Z')
RESERVED = frozenset({
    'admin', 'administrator', 'root', 'support', 'owner', 'operator', 'staff', 'moderator',
    'system', 'security', 'help', 'info', 'api', 'relay', 'bot', 'telegram', 'node-plane',
    'nodeplane', 'node_plane', 'me', 'null', 'none', 'undefined', 'anonymous', 'guest', 'user',
})
CHANGE_EVERY = timedelta(days=30)
HOLD = timedelta(days=30)


def _now():
    return datetime.now(timezone.utc)


def checked(login):
    """The login as typed and its key; raises when it breaks the rules."""
    if not isinstance(login, str) or not PATTERN.fullmatch(login):
        raise AccessDenied('login_invalid', 422)
    key = login.lower()
    if key in RESERVED:
        raise AccessDenied('login_reserved', 422)
    return login, key


def current(conn, account_id):
    row = conn.execute('SELECT login FROM backend_logins WHERE account_id=? AND released_at IS NULL',
                       (account_id,)).fetchone()
    return row['login'] if row else None


def change_available_at(conn, account_id, now=None):
    """When the account may change its own login, or None if it may now."""
    row = conn.execute('''SELECT MAX(claimed_at) AS at FROM backend_logins
        WHERE account_id=? AND kind='change' ''', (account_id,)).fetchone()
    if not row or not row['at']:
        return None
    at = datetime.fromisoformat(row['at']) + CHANGE_EVERY
    return at.isoformat() if at > (now or _now()) else None


def available(conn, key, account_id, now=None):
    """Whether `account_id` may take the login with this key."""
    row = conn.execute('SELECT account_id, released_at FROM backend_logins WHERE key=?', (key,)).fetchone()
    if row is None or row['account_id'] == account_id:
        return True
    return row['released_at'] is not None and \
        datetime.fromisoformat(row['released_at']) + HOLD <= (now or _now())


def claim(conn, account_id, login, kind, now=None):
    """Makes `login` the account's current login; its previous one is held."""
    now = now or _now()
    login, key = checked(login)
    if not available(conn, key, account_id, now):
        raise AccessDenied('login_taken', 409)
    previous = conn.execute('SELECT key FROM backend_logins WHERE account_id=? AND released_at IS NULL',
                            (account_id,)).fetchone()
    if previous and previous['key'] == key:
        # Only the case changes; the login stays the same.
        conn.execute('UPDATE backend_logins SET login=? WHERE key=?', (login, key))
        return login
    stamp = now.isoformat()
    if previous:
        conn.execute('UPDATE backend_logins SET released_at=? WHERE key=?', (stamp, previous['key']))
    # An expired hold of another account, or this account's own held login.
    conn.execute('DELETE FROM backend_logins WHERE key=?', (key,))
    conn.execute('''INSERT INTO backend_logins(key, login, account_id, kind, claimed_at)
        VALUES (?, ?, ?, ?, ?)''', (key, login, account_id, kind, stamp))
    return login


def set_own(conn, account_id, login, now=None):
    """The account sets its first login or changes it, at most once in CHANGE_EVERY."""
    now = now or _now()
    had = current(conn, account_id)
    if had is not None and had.lower() != str(login).lower() and change_available_at(conn, account_id, now):
        raise AccessDenied('login_change_too_soon', 409)
    return claim(conn, account_id, login, 'first' if had is None else 'change', now)


def release(conn, account_id, now=None):
    """The account gives up its login, which stays held for HOLD (deletion)."""
    conn.execute('UPDATE backend_logins SET released_at=? WHERE account_id=? AND released_at IS NULL',
                 ((now or _now()).isoformat(), account_id))


def suggestion(conn, account_id, username, now=None):
    """The Telegram username as a login, when it fits the rules and is free."""
    if not username:
        return None
    try:
        login, key = checked(username)
    except AccessDenied:
        return None
    return login if available(conn, key, account_id, now) else None
