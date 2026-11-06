"""Invites (see r0043).

An administrator creates a one-time link that expires. A `new` invite
registers an account without an access request and applies its template:
a VPN profile with the default access, and a tariff's access and term if one
is named. The template is applied on behalf of the invite's creator with the
rights the creator has at that moment, so an invite outlives no demotion. An
`account` invite names an existing account, which uses it to set its login
and password and is signed in; a password it sets replaces the old one and
ends the other sessions, so the same invite recovers a lost password.

Invites give member rights only; staff roles are assigned afterwards.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import re
import secrets
from uuid import UUID, uuid4

from . import logins, sessions
from .authorization import AccessDenied, PrincipalKind, require_permission

TOKEN = re.compile(r'inv_[A-Za-z0-9_-]{43}\Z')
DAYS = (1, 30)
DEFAULT_DAYS = 7
ACTIVE_LIMIT = 200
NOTE_LENGTH = 128


def _now():
    return datetime.now(timezone.utc)


def _digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _bound(conn):
    """A database whose transactions are the one already open."""
    class Bound:
        @contextmanager
        def connect(self):
            yield conn
        transaction = connect
    return Bound()


def status(row, now=None):
    now = now or _now()
    if row['revoked_at']:
        return 'revoked'
    if row['used_at']:
        return 'used'
    return 'expired' if datetime.fromisoformat(row['expires_at']) <= now else 'active'


def public(row, now=None):
    return {'id': row['id'], 'kind': row['kind'], 'account_id': row['account_id'], 'vpn': bool(row['vpn']),
            'tariff_id': row['tariff_id'], 'note': row['note'], 'created_by': row['created_by'],
            'created_at': row['created_at'], 'expires_at': row['expires_at'], 'used_at': row['used_at'],
            'used_account_id': row['used_account_id'], 'status': status(row, now)}


def cabinet_link(conn, token):
    """The link into the web cabinet, when there is a relay. The token is in
    the fragment, so it reaches no server log."""
    from .passkeys import relying_party
    try:
        _, origin = relying_party(conn)
    except AccessDenied:
        return None
    return f'{origin}/invite#{token}'


def create(db, actor, *, account_id=None, vpn=False, tariff_id=None, days=DEFAULT_DAYS, note=None, now=None):
    """A new invite and its token, which is shown only now."""
    now = now or _now()
    require_permission(actor, 'accounts.manage')
    if type(vpn) is not bool or type(days) is not int or not DAYS[0] <= days <= DAYS[1]:
        raise AccessDenied('invalid_input', 422)
    note = (note or '').strip()
    if len(note) > NOTE_LENGTH or any(c < ' ' for c in note):
        raise AccessDenied('invalid_input', 422)
    if account_id is not None and (vpn or tariff_id is not None):
        # An existing account keeps the access it has.
        raise AccessDenied('invalid_input', 422)
    if account_id is not None and actor.principal.kind == PrincipalKind.ADAPTER:
        # It signs in as that account: a secret, so not through Telegram.
        raise AccessDenied('invite_not_through_adapter', 403)
    if tariff_id is not None and not vpn:
        raise AccessDenied('invalid_input', 422)
    if vpn:
        require_permission(actor, 'profiles.manage')
    with db.transaction() as conn:
        if account_id is not None:
            row = conn.execute('SELECT status FROM backend_accounts WHERE id=?', (str(account_id),)).fetchone()
            if row is None or conn.execute('SELECT 1 FROM backend_account_deletions WHERE account_id=?',
                                           (str(account_id),)).fetchone():
                raise AccessDenied('resource_not_found', 404)
            if row['status'] != 'approved':
                raise AccessDenied('account_disabled', 409)
        if tariff_id is not None:
            try:
                tariff_id = str(UUID(str(tariff_id)))
            except ValueError:
                raise AccessDenied('invalid_input', 422) from None
            tariff = conn.execute('SELECT access_json FROM backend_tariffs WHERE id=?', (tariff_id,)).fetchone()
            if tariff is None:
                raise AccessDenied('tariff_not_found', 404)
            if tariff['access_json'] is not None:
                require_permission(actor, 'grants.manage')
        active = conn.execute('''SELECT COUNT(*) AS count FROM backend_invites
            WHERE used_at IS NULL AND revoked_at IS NULL AND expires_at > ?''', (now.isoformat(),)).fetchone()
        if active['count'] >= ACTIVE_LIMIT:
            raise AccessDenied('invite_limit', 409)
        invite_id, token = uuid4().hex, 'inv_' + secrets.token_urlsafe(32)
        conn.execute('''INSERT INTO backend_invites(id, token_hash, kind, account_id, vpn, tariff_id, note,
            created_by, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (invite_id, _digest(token), 'account' if account_id else 'new', str(account_id) if account_id else None,
             int(vpn), tariff_id, note, actor.account.id, now.isoformat(), (now + timedelta(days=days)).isoformat()))
        row = conn.execute('SELECT * FROM backend_invites WHERE id=?', (invite_id,)).fetchone()
        return {**public(row, now), 'token': token, 'cabinet_link': cabinet_link(conn, token)}


def listed(db, actor, now=None):
    """The latest invites, newest first."""
    require_permission(actor, 'accounts.manage')
    with db.connect() as conn:
        rows = conn.execute('SELECT * FROM backend_invites ORDER BY created_at DESC LIMIT 100').fetchall()
    return [public(row, now) for row in rows]


def get(db, actor, invite_id, now=None):
    require_permission(actor, 'accounts.manage')
    with db.connect() as conn:
        row = conn.execute('SELECT * FROM backend_invites WHERE id=?', (str(invite_id),)).fetchone()
    if row is None:
        raise AccessDenied('resource_not_found', 404)
    return public(row, now)


def revoke(db, actor, invite_id, now=None):
    require_permission(actor, 'accounts.manage')
    now = now or _now()
    with db.transaction() as conn:
        row = conn.execute('SELECT * FROM backend_invites WHERE id=?', (str(invite_id),)).fetchone()
        if row is None:
            raise AccessDenied('resource_not_found', 404)
        if status(row, now) == 'active':
            conn.execute('UPDATE backend_invites SET revoked_at=? WHERE id=?', (now.isoformat(), row['id']))
        return public(conn.execute('SELECT * FROM backend_invites WHERE id=?', (row['id'],)).fetchone(), now)


def _find(conn, token, source, now):
    """The active invite of the token. Wrong tokens count toward the source's limit."""
    scopes = (sessions.source_scope(source),)
    sessions.limited(conn, scopes, now)
    row = conn.execute('SELECT * FROM backend_invites WHERE token_hash=?', (_digest(token),)).fetchone() \
        if isinstance(token, str) and TOKEN.fullmatch(token) else None
    if row is None or status(row, now) != 'active':
        return None
    return row


def _refused(db, source, now):
    with db.transaction() as conn:
        sessions.failed(conn, (sessions.source_scope(source),), now)
    raise AccessDenied('invite_invalid', 404)


def preview(db, token, *, source, now=None):
    """What the invite does, before it is used."""
    now = now or _now()
    with db.transaction() as conn:
        row = _find(conn, token, source, now)
        if row is not None:
            tariff = conn.execute('SELECT title FROM backend_tariffs WHERE id=?', (row['tariff_id'],)).fetchone() \
                if row['tariff_id'] else None
            login = logins.current(conn, row['account_id']) if row['account_id'] else None
            return {'kind': row['kind'], 'expires_at': row['expires_at'], 'vpn': bool(row['vpn']),
                    'tariff': tariff['title'] if tariff else None, 'login': login,
                    'has_password': sessions.has_password(conn, row['account_id']) if row['account_id'] else False}
    _refused(db, source, now)


def _apply(conn, row, account_id, now):
    """The template, on behalf of the creator as the creator is now."""
    if not row['vpn']:
        return
    from .authorization import Principal, resolve_actor
    from .credentials import PERMISSIONS
    from .identity_repository import SQLIdentityRepository
    from .profiles import ProfileRepository
    local = _bound(conn)
    try:
        creator = resolve_actor(Principal('invite:' + row['id'], PrincipalKind.ACCOUNT, PERMISSIONS,
                                          account_id=row['created_by']), SQLIdentityRepository(local))
        require_permission(creator, 'profiles.manage')
    except AccessDenied:
        raise AccessDenied('invite_creator_unavailable', 409) from None
    profile_id = ProfileRepository.ensure_account_profile_in_transaction(conn, account_id)
    if row['tariff_id']:
        tariff = conn.execute('SELECT duration_days, access_json FROM backend_tariffs WHERE id=?',
                              (row['tariff_id'],)).fetchone()
        if tariff is None:
            raise AccessDenied('tariff_not_found', 404)
        from .profile_commands import ProfileCommands
        revision = conn.execute('SELECT desired_revision FROM backend_profiles WHERE id=?',
                                (profile_id,)).fetchone()['desired_revision']
        ProfileCommands(local).execute(creator, str(uuid4()), action='tariff', profile_id=profile_id,
            revision=revision, values={'tariff_id': row['tariff_id'],
                                       'expiry': 'keep' if tariff['duration_days'] is None else 'start',
                                       'apply_access': tariff['access_json'] is not None})


def _use(conn, row, account_id, now):
    if conn.execute('UPDATE backend_invites SET used_at=?, used_account_id=? WHERE id=? AND used_at IS NULL '
                    'AND revoked_at IS NULL RETURNING id', (now.isoformat(), account_id, row['id'])).fetchone() is None:
        raise AccessDenied('invite_invalid', 404)


def _new_account(conn, display_name=None):
    account_id = str(uuid4())
    conn.execute("INSERT INTO backend_accounts(id, status, display_name) VALUES (?, 'approved', ?)",
                 (account_id, display_name))
    return account_id


def accept(db, token, *, login=None, password=None, device=None, source, now=None):
    """Uses the invite in the web cabinet and signs in. A `new` invite needs a
    login; a password is optional, since a passkey can be added right after."""
    now = now or _now()
    with db.transaction() as conn:
        row = _find(conn, token, source, now)
        if row is not None:
            if row['kind'] == 'new':
                if not login:
                    raise AccessDenied('login_required', 422)
                checked, _ = logins.checked(login)
                account_id = _new_account(conn, checked)
                logins.claim(conn, account_id, checked, 'first', now)
                _apply(conn, row, account_id, now)
            else:
                account_id = row['account_id']
                account = conn.execute(f'''SELECT a.id FROM backend_accounts a WHERE a.id=?
                    AND {sessions.ACTIVE_ACCOUNT_SQL}''', (account_id,)).fetchone()
                if account is None:
                    raise AccessDenied('invite_invalid', 404)
                if logins.current(conn, account_id) is None:
                    if not login:
                        raise AccessDenied('login_required', 422)
                    logins.set_own(conn, account_id, login, now)
            if password is not None:
                sessions.checked_password(password, logins.current(conn, account_id))
                # The invite stands in for the current password.
                conn.execute('DELETE FROM backend_passwords WHERE account_id=?', (account_id,))
                sessions.set_password(conn, account_id, password, now=now)
                sessions.revoke_all(conn, account_id, now=now)
            _use(conn, row, account_id, now)
            from .accounts import AccountService
            return {'account': AccountService.public(AccountService._read(conn, account_id)),
                    'session': sessions.open_session(conn, account_id, 'invite', device, now)}
    _refused(db, source, now)


def accept_telegram(db, token, subject, *, login=None, source='telegram', now=None):
    """Uses a `new` invite in the bot: the account comes with the person's Telegram."""
    now = now or _now()
    with db.transaction() as conn:
        row = _find(conn, token, source, now)
        if row is not None:
            if row['kind'] != 'new':
                raise AccessDenied('invite_not_for_telegram', 409)
            if conn.execute("SELECT 1 FROM backend_external_identities WHERE provider='telegram' AND subject=?",
                            (subject,)).fetchone():
                raise AccessDenied('invite_already_registered', 409)
            account_id = _new_account(conn)
            conn.execute("INSERT INTO backend_external_identities(provider, subject, account_id) VALUES ('telegram', ?, ?)",
                         (subject, account_id))
            from db.migration_revisions.r0028_account_details import backfill
            backfill(conn, account_id)
            from .account_channels import attach
            attach(conn, account_id, 'telegram', subject)
            if login:
                logins.claim(conn, account_id, logins.checked(login)[0], 'first', now)
            _apply(conn, row, account_id, now)
            _use(conn, row, account_id, now)
            # The bot shows the member's menu now.
            conn.execute('DELETE FROM backend_telegram_menu_refresh WHERE subject=?', (subject,))
            from .accounts import AccountService
            return {'account': AccountService.public(AccountService._read(conn, account_id))}
    _refused(db, source, now)
