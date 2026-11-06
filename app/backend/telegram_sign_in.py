"""Telegram in the web cabinet (see r0045): signing in, and moving a Telegram
between accounts.

Telegram signs what it hands out with the bot's token, and the controller
checks it: the Login Widget's fields with SHA-256 of the token as the key,
the Mini App's `initData` with HMAC("WebAppData", token). Data older than
`MAX_AGE` is refused, and each signed set is used once.

A Telegram belongs to one account. An account signed in to the cabinet links
one by proving it (Login Widget or Mini App) or with a code the bot gives
out. If that Telegram already belongs to another account, it moves only when
that account is empty, with no service access, request or staff role; the
empty account is then deleted. Otherwise the move is refused: that other
account would keep its access with no way to sign in. Accounts and their
access are never merged here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
from urllib.parse import parse_qsl
from uuid import uuid4

from . import sessions
from .authorization import AccessDenied

MAX_AGE = timedelta(minutes=10)
WEBAPP_MAX_AGE = timedelta(hours=1)
LINK_TTL = timedelta(minutes=10)
CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'
CODE_LENGTH = 8


def _now():
    return datetime.now(timezone.utc)


def bot_token():
    """The bot's token from the controller's `.env`, as the bot reads it."""
    try:
        import config
        for line in (Path(config.SHARED_ROOT) / '.env').read_text().splitlines():
            if line.startswith('BOT_TOKEN='):
                return line.removeprefix('BOT_TOKEN=').strip() or None
    except (ImportError, OSError, AttributeError):
        pass
    return os.environ.get('BOT_TOKEN') or None


def _checked_token(conn):
    from .account_channels import enabled
    token = bot_token()
    if 'telegram' not in enabled(conn) or not token:
        raise AccessDenied('telegram_disabled', 409)
    return token


def _fresh(auth_date, now, max_age):
    try:
        signed = datetime.fromtimestamp(int(auth_date), timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return False
    return now - max_age <= signed <= now + timedelta(minutes=1)


def verified(kind, data, token, now):
    """The Telegram user `{id, username}` the data is signed for, and its hash; None if it is not."""
    if kind == 'login' and isinstance(data, dict):
        fields = {key: str(value) for key, value in data.items() if key != 'hash' and value is not None}
        secret, max_age, user = hashlib.sha256(token.encode()).digest(), MAX_AGE, fields
    elif kind == 'webapp' and isinstance(data, str) and len(data) <= 4096:
        fields = dict(parse_qsl(data, keep_blank_values=True))
        secret, max_age = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest(), WEBAPP_MAX_AGE
        try:
            user = json.loads(fields.get('user', ''))
        except ValueError:
            return None
        data = fields
    else:
        return None
    given = data.get('hash')
    fields.pop('hash', None)
    check = '\n'.join(f'{key}={fields[key]}' for key in sorted(fields))
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not isinstance(given, str) or not hmac.compare_digest(expected, given) \
            or not _fresh(fields.get('auth_date'), now, max_age):
        return None
    try:
        user_id = int(user['id'])
    except (KeyError, TypeError, ValueError):
        return None
    from .authorization import validate_telegram_id
    try:
        validate_telegram_id(user_id)
    except AccessDenied:
        return None
    username = user.get('username')
    return {'id': user_id, 'username': username if isinstance(username, str) else None}, given


def _use(conn, signature, now):
    """Refuses signed data already accepted."""
    conn.execute('DELETE FROM backend_telegram_auth_used WHERE expires_at<=?', (now.isoformat(),))
    if conn.execute('SELECT 1 FROM backend_telegram_auth_used WHERE hash=?', (signature,)).fetchone():
        return False
    conn.execute('INSERT INTO backend_telegram_auth_used(hash, expires_at) VALUES (?, ?)',
                 (signature, (now + WEBAPP_MAX_AGE + timedelta(minutes=1)).isoformat()))
    return True


def _proven(db, kind, data, source, now):
    """The Telegram user the data proves, with the attempt counted if it does not."""
    scopes = (sessions.source_scope(source),)
    with db.transaction() as conn:
        sessions.limited(conn, scopes, now)
        token = _checked_token(conn)
        result = verified(kind, data, token, now)
        if result is not None and _use(conn, result[1], now):
            return result[0]
    with db.transaction() as conn:
        sessions.failed(conn, scopes, now)
    raise AccessDenied('telegram_auth_invalid', 401)


def sign_in(db, kind, data, *, device, source, now=None):
    """Tokens of a new session of the account the Telegram belongs to."""
    now = now or _now()
    user = _proven(db, kind, data, source, now)
    with db.transaction() as conn:
        row = conn.execute(f'''SELECT a.id FROM backend_external_identities i JOIN backend_accounts a ON a.id = i.account_id
            WHERE i.provider = 'telegram' AND i.subject = ? AND {sessions.ACTIVE_ACCOUNT_SQL}''',
            (str(user['id']),)).fetchone()
        if row is None:
            # Not an error of the data: this Telegram has no account here yet.
            raise AccessDenied('telegram_not_registered', 404)
        return sessions.open_session(conn, row['id'], 'telegram', device, now)


def _owner(conn, subject):
    return conn.execute('''SELECT a.id, a.role FROM backend_external_identities i JOIN backend_accounts a ON a.id = i.account_id
        WHERE i.provider = 'telegram' AND i.subject = ?''', (subject,)).fetchone()


def _empty(conn, account_id):
    from .registrations import WITHOUT_ACCESS_SQL
    return conn.execute(f'SELECT 1 FROM backend_accounts a WHERE a.id = ? AND {WITHOUT_ACCESS_SQL}',
                        (account_id,)).fetchone() is not None


def _state(conn, account_id, subject):
    own = conn.execute("SELECT subject FROM backend_external_identities WHERE provider='telegram' AND account_id=?",
                       (account_id,)).fetchone()
    if own is not None:
        return 'own' if own['subject'] == subject else 'account_has_telegram', None
    owner = _owner(conn, subject)
    if owner is None:
        return 'free', None
    return ('other_empty' if _empty(conn, owner['id']) else 'other_with_access'), owner['id']


def _preview(conn, link_id, account_id, subject):
    from .logins import current
    state, other = _state(conn, account_id, subject)
    details = conn.execute('SELECT username FROM backend_telegram_identity_details WHERE subject=?',
                           (subject,)).fetchone()
    return {'link_id': link_id, 'telegram_user_id': int(subject),
            'username': details['username'] if details else None, 'state': state,
            'other_login': current(conn, other) if other else None}


def _remember(conn, user, now):
    """Keeps the proven Telegram's username for the preview."""
    conn.execute('''INSERT INTO backend_telegram_identity_details(subject, username, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(subject) DO UPDATE SET username = COALESCE(excluded.username, backend_telegram_identity_details.username),
        updated_at = excluded.updated_at''', (str(user['id']), user['username'], now.isoformat()))


def link_preview(db, account_id, *, kind=None, data=None, code=None, source, now=None):
    """What linking this Telegram to the account would do, and the link to confirm."""
    now = now or _now()
    if code is not None:
        scopes = (sessions.source_scope(source),)
        normalized = code.strip().upper().replace('-', '') if isinstance(code, str) else ''
        with db.transaction() as conn:
            sessions.limited(conn, scopes, now)
            row = conn.execute('''SELECT * FROM backend_telegram_links WHERE code_hash=? AND account_id IS NULL
                AND expires_at>?''', (_digest(normalized), now.isoformat())).fetchone() if normalized else None
            if row is not None:
                # The code is used up; the link now belongs to this account.
                conn.execute('UPDATE backend_telegram_links SET account_id=?, code_hash=NULL WHERE id=?',
                             (account_id, row['id']))
                return _preview(conn, row['id'], account_id, row['subject'])
        with db.transaction() as conn:
            sessions.failed(conn, scopes, now)
        raise AccessDenied('telegram_code_invalid', 404)
    user = _proven(db, kind, data, source, now)
    with db.transaction() as conn:
        _remember(conn, user, now)
        link_id = uuid4().hex
        conn.execute('''INSERT INTO backend_telegram_links(id, account_id, subject, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)''', (link_id, account_id, str(user['id']), now.isoformat(),
                                       (now + LINK_TTL).isoformat()))
        return _preview(conn, link_id, account_id, str(user['id']))


def _digest(code):
    return hashlib.sha256(code.encode()).hexdigest()


def link_code(db, subject, now=None):
    """A code for the cabinet: the bot's person moves its Telegram to the
    cabinet account the code is entered in."""
    now = now or _now()
    with db.transaction() as conn:
        _checked_token(conn)
        owner = _owner(conn, subject)
        if owner is not None and not _empty(conn, owner['id']):
            raise AccessDenied('telegram_account_has_access', 409)
        conn.execute('DELETE FROM backend_telegram_links WHERE subject=? AND account_id IS NULL', (subject,))
        conn.execute('DELETE FROM backend_telegram_links WHERE expires_at<=?', (now.isoformat(),))
        code = ''.join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        expires = now + LINK_TTL
        conn.execute('''INSERT INTO backend_telegram_links(id, code_hash, subject, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)''', (uuid4().hex, _digest(code), subject, now.isoformat(), expires.isoformat()))
    return {'code': f'{code[:4]}-{code[4:]}', 'expires_at': expires.isoformat()}


def link_confirm(db, account_id, link_id, now=None):
    """Links the Telegram: free, or moved from an empty account, which is deleted."""
    now = now or _now()
    with db.transaction() as conn:
        row = conn.execute('SELECT * FROM backend_telegram_links WHERE id=? AND account_id=? AND expires_at>?',
                           (link_id, account_id, now.isoformat())).fetchone() if isinstance(link_id, str) else None
        if row is None:
            raise AccessDenied('telegram_link_invalid', 404)
        subject = row['subject']
        # Concurrent links of the same Telegram wait for each other.
        conn.execute('UPDATE backend_accounts SET revision=revision+1 WHERE id=?', (account_id,))
        state, other = _state(conn, account_id, subject)
        if state in {'own', 'account_has_telegram'}:
            raise AccessDenied('telegram_already_linked' if state == 'own' else 'account_has_telegram', 409)
        if state == 'other_with_access':
            raise AccessDenied('telegram_account_has_access', 409)
        conn.execute('DELETE FROM backend_telegram_links WHERE id=?', (row['id'],))
        if other is not None:
            conn.execute("DELETE FROM backend_external_identities WHERE provider='telegram' AND subject=?", (subject,))
            conn.execute("DELETE FROM backend_account_channels WHERE account_id=? AND channel='telegram'", (other,))
            from .account_deletions import begin, settle
            begin(conn, other)
            settle(conn)
        conn.execute("INSERT INTO backend_external_identities(provider, subject, account_id) VALUES ('telegram', ?, ?)",
                     (subject, account_id))
        from db.migration_revisions.r0028_account_details import backfill
        backfill(conn, account_id)
        from .account_channels import attach
        attach(conn, account_id, 'telegram', subject)
        # The bot shows this account's menu to the person now.
        revision = conn.execute('SELECT revision FROM backend_accounts WHERE id=?', (account_id,)).fetchone()['revision']
        conn.execute('''INSERT INTO backend_account_menu_refresh(account_id, revision) VALUES (?, ?)
            ON CONFLICT(account_id) DO UPDATE SET revision=excluded.revision''', (account_id, revision))
        conn.execute('DELETE FROM backend_telegram_menu_refresh WHERE subject=?', (subject,))
        from .accounts import AccountService
        return AccountService.public(AccountService._read(conn, account_id))
