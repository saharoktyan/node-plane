"""Merging two accounts of one person, by request (see r0046).

The account to be merged in (the other) hands out a code, signed in in the
bot or the web cabinet; the main account enters it. Holding both proves
they are one person's. An administrator then sees each account on its own
and what the merge does, and approves or rejects it.

On approval, in one transaction, the main account gets the other's VPN
profile, Telegram with its channel, and its password and login when it has
none, and the other account is deleted. Passkeys do not move: each names
its account in the authenticator, so the other's are removed. When both
have a VPN profile, the administrator picks which stays; the other profile
is deleted as any profile is, revoked on its nodes, and the other account is
deleted once that settles. Staff accounts are not merged in.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import secrets
from uuid import uuid4

from . import sessions
from .authorization import AccessDenied, require_permission

CODE_TTL = timedelta(minutes=10)
CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'
CODE_LENGTH = 8


def _now():
    return datetime.now(timezone.utc)


def _digest(code):
    return hashlib.sha256(code.encode()).hexdigest()


def _active(conn, account_id):
    return conn.execute(f'SELECT a.* FROM backend_accounts a WHERE a.id=? AND {sessions.ACTIVE_ACCOUNT_SQL}',
                        (account_id,)).fetchone()


def _pending(conn, account_id):
    return conn.execute('''SELECT * FROM backend_merge_requests WHERE status='pending'
        AND (main_account_id=? OR other_account_id=?)''', (account_id, account_id)).fetchone()


def code(db, account_id, now=None):
    """A code for the main account to enter: this account is merged into it."""
    now = now or _now()
    with db.transaction() as conn:
        account = _active(conn, account_id)
        if account is None:
            raise AccessDenied('resource_not_found', 404)
        if account['role'] != 'member':
            raise AccessDenied('merge_staff_account', 409)
        if _pending(conn, account_id):
            raise AccessDenied('merge_already_pending', 409)
        value = ''.join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        expires = now + CODE_TTL
        conn.execute('DELETE FROM backend_merge_codes WHERE account_id=? OR expires_at<=?',
                     (account_id, now.isoformat()))
        conn.execute('INSERT INTO backend_merge_codes(code_hash, account_id, expires_at) VALUES (?, ?, ?)',
                     (_digest(value), account_id, expires.isoformat()))
    return {'code': f'{value[:4]}-{value[4:]}', 'expires_at': expires.isoformat()}


def request(db, main_account_id, value, *, source, now=None):
    """The main account asks to take the code's account over."""
    now = now or _now()
    scopes = (sessions.source_scope(source),)
    normalized = value.strip().upper().replace('-', '') if isinstance(value, str) else ''
    with db.transaction() as conn:
        sessions.limited(conn, scopes, now)
        row = conn.execute('SELECT * FROM backend_merge_codes WHERE code_hash=? AND expires_at>?',
                           (_digest(normalized), now.isoformat())).fetchone() if normalized else None
        if row is not None:
            conn.execute('DELETE FROM backend_merge_codes WHERE code_hash=?', (row['code_hash'],))
            other = row['account_id']
            if other == main_account_id:
                raise AccessDenied('merge_same_account', 409)
            if _active(conn, main_account_id) is None or _active(conn, other) is None:
                raise AccessDenied('resource_not_found', 404)
            if _pending(conn, main_account_id) or _pending(conn, other):
                raise AccessDenied('merge_already_pending', 409)
            request_id = uuid4().hex
            conn.execute('''INSERT INTO backend_merge_requests(id, main_account_id, other_account_id, status, created_at)
                VALUES (?, ?, ?, 'pending', ?)''', (request_id, main_account_id, other, now.isoformat()))
            return public(conn, conn.execute('SELECT * FROM backend_merge_requests WHERE id=?',
                                             (request_id,)).fetchone())
    with db.transaction() as conn:
        sessions.failed(conn, scopes, now)
    raise AccessDenied('merge_code_invalid', 404)


def _summary(conn, account_id):
    from .logins import current
    from .passkeys import count
    account = conn.execute('SELECT id, role, display_name FROM backend_accounts WHERE id=?', (account_id,)).fetchone()
    telegram = conn.execute('''SELECT i.subject, d.username FROM backend_external_identities i
        LEFT JOIN backend_telegram_identity_details d ON d.subject = i.subject
        WHERE i.provider='telegram' AND i.account_id=?''', (account_id,)).fetchone()
    profile = conn.execute('''SELECT p.id, p.display_name, p.expires_at,
        (SELECT COUNT(*) FROM backend_devices v WHERE v.profile_id = p.id AND v.status = 'active') AS devices,
        (SELECT t.title FROM backend_profile_tariffs pt JOIN backend_tariffs t ON t.id = pt.tariff_id
            WHERE pt.profile_id = p.id) AS tariff
        FROM backend_profiles p WHERE p.owner_account_id=?
        AND NOT EXISTS (SELECT 1 FROM backend_profile_deletions d WHERE d.profile_id = p.id)
        ORDER BY p.id LIMIT 1''', (account_id,)).fetchone()
    return {'id': account_id, 'login': current(conn, account_id), 'role': account['role'] if account else None,
            'display_name': account['display_name'] if account else None,
            'telegram_user_id': int(telegram['subject']) if telegram else None,
            'username': telegram['username'] if telegram else None,
            'vpn': dict(profile) if profile else None,
            'has_password': sessions.has_password(conn, account_id), 'passkeys': count(conn, account_id)}


def preview(conn, row):
    """Both accounts and what the merge does."""
    main, other = _summary(conn, row['main_account_id']), _summary(conn, row['other_account_id'])

    def moves(main_has, other_has):
        return None if not other_has else ('dropped' if main_has else 'moves')
    vpn = None
    if other['vpn'] and main['vpn']:
        vpn = 'choose'
    elif other['vpn']:
        vpn = 'moves'
    return {'main': main, 'other': other, 'result': {
        'vpn': vpn, 'telegram': moves(main['telegram_user_id'], other['telegram_user_id']),
        'password': moves(main['has_password'], other['has_password']),
        'login': moves(main['login'], other['login']), 'passkeys_removed': other['passkeys'],
        # The main account's VPN of an administrator never expires and is kept.
        'keep_main_only': main['role'] == 'admin'}}


def public(conn, row, *, with_preview=False):
    result = {'id': row['id'], 'main_account_id': row['main_account_id'],
              'other_account_id': row['other_account_id'], 'status': row['status'],
              'keep_profile': row['keep_profile'], 'created_at': row['created_at'], 'decided_at': row['decided_at']}
    if with_preview:
        result['preview'] = json.loads(row['preview_json']) if row['preview_json'] else preview(conn, row)
    return result


def own(db, account_id):
    with db.connect() as conn:
        rows = conn.execute('''SELECT * FROM backend_merge_requests WHERE main_account_id=? OR other_account_id=?
            ORDER BY created_at DESC LIMIT 20''', (account_id, account_id)).fetchall()
        return [public(conn, row, with_preview=True) for row in rows]


def cancel(db, account_id, request_id, now=None):
    now = now or _now()
    with db.transaction() as conn:
        row = conn.execute('''SELECT * FROM backend_merge_requests WHERE id=? AND status='pending'
            AND (main_account_id=? OR other_account_id=?)''', (request_id, account_id, account_id)).fetchone()
        if row is None:
            raise AccessDenied('resource_not_found', 404)
        conn.execute("UPDATE backend_merge_requests SET status='cancelled', decided_at=? WHERE id=?",
                     (now.isoformat(), row['id']))


def listed(db, actor):
    require_permission(actor, 'accounts.manage')
    with db.connect() as conn:
        rows = conn.execute('''SELECT * FROM backend_merge_requests
            ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, created_at DESC LIMIT 100''').fetchall()
        return [public(conn, row, with_preview=True) for row in rows]


def get(db, actor, request_id):
    require_permission(actor, 'accounts.manage')
    with db.connect() as conn:
        row = conn.execute('SELECT * FROM backend_merge_requests WHERE id=?', (request_id,)).fetchone()
        if row is None:
            raise AccessDenied('resource_not_found', 404)
        return public(conn, row, with_preview=True)


def _bound(conn):
    class Bound:
        @contextmanager
        def connect(self):
            yield conn
        transaction = connect
    return Bound()


def decide(db, actor, request_id, decision, *, keep_profile=None, now=None):
    """Approves (merging now) or rejects a pending request."""
    require_permission(actor, 'accounts.manage')
    require_permission(actor, 'profiles.manage')
    if decision not in {'approve', 'reject'} or keep_profile not in {None, 'main', 'other'}:
        raise AccessDenied('invalid_input', 422)
    now = now or _now()
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM backend_merge_requests WHERE id=? AND status='pending'",
                           (request_id,)).fetchone()
        if row is None:
            raise AccessDenied('resource_not_found', 404)
        main, other = row['main_account_id'], row['other_account_id']
        if actor.account.id in {main, other}:
            raise AccessDenied('merge_own_request', 403)
        if decision == 'reject':
            conn.execute('''UPDATE backend_merge_requests SET status='rejected', decided_at=?, decided_by=?
                WHERE id=?''', (now.isoformat(), actor.account.id, row['id']))
            return public(conn, conn.execute('SELECT * FROM backend_merge_requests WHERE id=?',
                                             (row['id'],)).fetchone(), with_preview=True)
        # Both accounts wait for the merge, and nothing changes them meanwhile.
        conn.execute('UPDATE backend_accounts SET revision=revision+1 WHERE id IN (?, ?)', (main, other))
        if _active(conn, main) is None or _active(conn, other) is None:
            raise AccessDenied('resource_not_found', 404)
        seen = preview(conn, row)
        if seen['other']['role'] != 'member':
            raise AccessDenied('merge_staff_account', 409)
        result = seen['result']
        if result['vpn'] == 'choose':
            if keep_profile is None:
                raise AccessDenied('merge_profile_choice_required', 422)
            if keep_profile == 'other' and result['keep_main_only']:
                raise AccessDenied('admin_access_permanent', 422)
        else:
            keep_profile = None
        _merge(conn, actor, seen, keep_profile, now)
        stored = {**seen, 'keep_profile': keep_profile}
        conn.execute('''UPDATE backend_merge_requests SET status='approved', keep_profile=?, preview_json=?,
            decided_at=?, decided_by=? WHERE id=?''', (keep_profile, json.dumps(stored, sort_keys=True),
                                                     now.isoformat(), actor.account.id, row['id']))
        return public(conn, conn.execute('SELECT * FROM backend_merge_requests WHERE id=?',
                                         (row['id'],)).fetchone(), with_preview=True)


def _owner(conn, profile_id, account_id):
    conn.execute('UPDATE backend_profiles SET owner_account_id=? WHERE id=?', (account_id, profile_id))
    conn.execute('UPDATE backend_service_profiles SET account_id=? WHERE id=?', (account_id, profile_id))


def _merge(conn, actor, seen, keep_profile, now):
    main, other = seen['main']['id'], seen['other']['id']
    result = seen['result']
    # Sign-in and channels the main account lacks.
    if result['telegram'] == 'moves':
        subject = str(seen['other']['telegram_user_id'])
        conn.execute("UPDATE backend_external_identities SET account_id=? WHERE provider='telegram' AND subject=?",
                     (main, subject))
        conn.execute("DELETE FROM backend_account_channels WHERE account_id=? AND channel='telegram'", (other,))
        from .account_channels import attach
        attach(conn, main, 'telegram', subject)
        from db.migration_revisions.r0028_account_details import backfill
        backfill(conn, main)
        conn.execute('DELETE FROM backend_telegram_menu_refresh WHERE subject=?', (subject,))
    if result['password'] == 'moves':
        conn.execute('UPDATE backend_passwords SET account_id=? WHERE account_id=?', (main, other))
    if result['login'] == 'moves':
        conn.execute('UPDATE backend_logins SET account_id=? WHERE account_id=? AND released_at IS NULL',
                     (main, other))
    # The other account's requests end with it.
    conn.execute('''UPDATE backend_access_requests SET status='rejected', decided_at=?, decided_by=?
        WHERE account_id=? AND status='pending' ''', (now.isoformat(), actor.account.id, other))
    drop = None
    if result['vpn'] == 'moves':
        _owner(conn, seen['other']['vpn']['id'], main)
    elif result['vpn'] == 'choose':
        kept, drop = ((seen['main']['vpn']['id'], seen['other']['vpn']['id']) if keep_profile == 'main'
                      else (seen['other']['vpn']['id'], seen['main']['vpn']['id']))
        # The kept profile is the main account's; the dropped one leaves with the other account.
        # An account owns one VPN profile, so a swap passes through no owner.
        conn.execute('UPDATE backend_profiles SET owner_account_id=NULL WHERE id=?', (drop,))
        _owner(conn, kept, main)
        _owner(conn, drop, other)
    for account_id in (main, other):
        revision = conn.execute('SELECT revision FROM backend_accounts WHERE id=?', (account_id,)).fetchone()['revision']
        conn.execute('''INSERT INTO backend_account_menu_refresh(account_id, revision) VALUES (?, ?)
            ON CONFLICT(account_id) DO UPDATE SET revision=excluded.revision''', (account_id, revision))
    if drop is not None:
        # Deleting the profile revokes it on its nodes and deletes its owner, the other account, after.
        from .profile_commands import ProfileCommands
        revision = conn.execute('SELECT desired_revision FROM backend_profiles WHERE id=?',
                                (drop,)).fetchone()['desired_revision']
        ProfileCommands(_bound(conn)).execute(actor, str(uuid4()), action='delete', profile_id=drop,
                                              revision=revision)
    else:
        from .account_deletions import begin, settle
        begin(conn, other)
        settle(conn)
