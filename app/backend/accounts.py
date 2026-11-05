"""Administrator account reads and guarded account-state changes."""
from __future__ import annotations

import json
import re
from contextlib import contextmanager
from uuid import UUID

from .authorization import AccessDenied, require_permission
from .profiles import _cursor, _page


def _key(value):
    try:
        return str(UUID(value))
    except (TypeError, ValueError, AttributeError):
        raise AccessDenied('invalid_idempotency_key', 422) from None


def _roles(conn, row):
    """The account's installation-wide roles, as the permissions see them."""
    from .authorization import Account, effective_roles
    from .identity_repository import account_roles
    return sorted(effective_roles(Account(row['id'], row['role'], row['status'], account_roles(conn, row))))


class AccountService:
    def __init__(self, db):
        self.db = db

    def initialize_schema(self):
        with self.db.transaction() as conn:
            from db.migration_revisions.r0007_account_menu_refresh import upgrade
            upgrade(conn)
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_account_commands (
                actor_account_id TEXT NOT NULL REFERENCES backend_accounts(id),
                command_key TEXT NOT NULL,
                target_account_id TEXT NOT NULL REFERENCES backend_accounts(id),
                input_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                PRIMARY KEY(actor_account_id, command_key)
            )''')

    @staticmethod
    def public(row):
        result = {'id': row['id'], 'role': row['role'], 'status': row['status'],
                'revision': row['revision'],
                'telegram_user_id': int(row['telegram_subject']) if row['telegram_subject'] else None}
        for key in ('display_name', 'username', 'first_name', 'last_name', 'language_code', 'locale', 'locale_selected'):
            if key in row.keys():
                result[key] = bool(row[key]) if key == 'locale_selected' else row[key]
        if 'profile_id' in row.keys():
            result['profile_id'] = row['profile_id']
        if 'roles' in row.keys():
            result['roles'] = row['roles']
        result['deleted'] = bool(row['deleted']) if 'deleted' in row.keys() else False
        return result

    @staticmethod
    def _read(conn, account_id):
        row = conn.execute('''SELECT a.id, a.role, a.status, a.revision,
            i.subject AS telegram_subject, a.display_name, d.username, d.first_name, d.last_name,
            d.language_code, a.locale, a.locale_selected,
            (SELECT p.id FROM backend_profiles p WHERE p.owner_account_id=a.id ORDER BY p.id LIMIT 1) AS profile_id
            FROM backend_accounts a
            LEFT JOIN backend_external_identities i ON i.account_id = a.id AND i.provider = 'telegram'
            LEFT JOIN backend_telegram_identity_details d ON d.subject = i.subject
            WHERE a.id = ?''', (account_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result['roles'] = _roles(conn, row)
        deletion = conn.execute('SELECT telegram_json,completed_at FROM backend_account_deletions WHERE account_id=?', (account_id,)).fetchone()
        if deletion:
            saved = json.loads(deletion['telegram_json'])
            result['telegram_subject'] = str(saved['telegram_user_id']) if saved.get('telegram_user_id') else None
            for field in ('username','first_name','last_name','language_code','locale','locale_selected'):
                result[field] = saved.get(field)
            result['deleted'] = deletion['completed_at'] is not None
        return result

    def get(self, actor, account_id):
        require_permission(actor, 'accounts.manage')
        with self.db.connect() as conn:
            row = self._read(conn, account_id)
        if row is None:
            raise AccessDenied('resource_not_found', 404)
        return self.public(row)

    def list(self, actor, *, limit=25, cursor=None):
        require_permission(actor, 'accounts.manage')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AccessDenied('invalid_input', 422)
        after = _cursor(cursor, 'accounts')
        with self.db.connect() as conn:
            rows = conn.execute('''SELECT a.id, a.role, a.status, a.revision,
                i.subject AS telegram_subject, a.display_name, d.username, d.first_name, d.last_name,
                d.language_code, a.locale, a.locale_selected
                FROM backend_accounts a
                LEFT JOIN backend_external_identities i ON i.account_id = a.id AND i.provider = 'telegram'
                LEFT JOIN backend_telegram_identity_details d ON d.subject = i.subject
                WHERE a.id > ? AND NOT EXISTS (SELECT 1 FROM backend_account_deletions x WHERE x.account_id=a.id) ORDER BY a.id LIMIT ?''', (after, limit + 1)).fetchall()
            rows = [{**dict(row), 'roles': _roles(conn, row)} for row in rows]
        return _page([self.public(row) for row in rows], limit, 'accounts', 'id')

    def provision_profile(self, actor, identity, command_key, *, display_name=None):
        """Provision one account and its default VPN profile as one command.

        The account is known by its Telegram ID, or only by name when the
        person has no Telegram (Workstation-only or a future web cabinet).
        """
        require_permission(actor, 'accounts.manage')
        require_permission(actor, 'profiles.manage')
        key = _key(command_key)
        if (identity is None) == (display_name is None):
            raise AccessDenied('invalid_input', 422)
        if display_name is not None:
            if (not isinstance(display_name, str) or not 0 < len(display_name.strip()) <= 128
                    or any(c < ' ' for c in display_name)):
                raise AccessDenied('invalid_input', 422)
            display_name = display_name.strip()
            input_json = json.dumps({'action': 'provision_profile', 'display_name': display_name}, sort_keys=True)
            return self._provision(actor, key, input_json, display_name=display_name)
        if not isinstance(identity, str):
            raise AccessDenied('invalid_telegram_identity', 422)
        identity = identity.strip()
        if not re.fullmatch(r'[1-9][0-9]{0,15}|@[A-Za-z0-9_]{5,32}', identity):
            raise AccessDenied('invalid_telegram_identity', 422)
        input_json = json.dumps({'action': 'provision_profile', 'identity': identity.lower()}, sort_keys=True)
        return self._provision(actor, key, input_json, identity=identity)

    def _provision(self, actor, key, input_json, *, identity=None, display_name=None):
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            conn.execute('UPDATE backend_account_guard SET revision=revision+1 WHERE id=1')
            current = conn.execute('SELECT role,status FROM backend_accounts WHERE id=?', (actor.account.id,)).fetchone()
            if not current or (current['role'], current['status']) != ('admin', 'approved'):
                raise AccessDenied('permission_denied')
            previous = conn.execute('SELECT input_json,result_json FROM backend_account_commands WHERE actor_account_id=? AND command_key=?', (actor.account.id, key)).fetchone()
            if previous:
                if previous['input_json'] != input_json:
                    raise AccessDenied('idempotency_conflict', 409)
                return json.loads(previous['result_json'])
            class BoundDatabase:
                @contextmanager
                def connect(self):
                    yield conn
                transaction = connect
            local = BoundDatabase()
            from .identity_repository import SQLIdentityRepository
            identities = SQLIdentityRepository(local)
            if display_name is not None:
                account = identities.create_account(status='approved')
                conn.execute('UPDATE backend_accounts SET display_name=? WHERE id=?', (display_name, account.id))
                from .profiles import ProfileRepository
                profile_id = ProfileRepository.ensure_account_profile_in_transaction(conn, account.id)
                profile = conn.execute('SELECT * FROM backend_profiles WHERE id=?', (profile_id,)).fetchone()
                result = {'account': self.public(self._read(conn, account.id)),
                          'profile': ProfileRepository.public(profile), 'created': True}
                conn.execute('INSERT INTO backend_account_commands VALUES (?,?,?,?,?)',
                             (actor.account.id, key, account.id, input_json, json.dumps(result, sort_keys=True)))
                return result
            if identity.startswith('@'):
                raise AccessDenied('telegram_id_required', 422)
            else:
                user_id = int(identity)
            from .authorization import validate_telegram_id
            validate_telegram_id(user_id)
            account = identities.find_telegram_account(user_id)
            created = account is None
            account = account or identities._resolve(conn, user_id)
            if account.status == 'disabled':
                raise AccessDenied('account_disabled', 409)
            from .profiles import ProfileRepository
            pending = conn.execute("""SELECT id FROM backend_access_requests WHERE status='pending'
                AND (account_id=? OR (account_id IS NULL AND telegram_subject=?))""", (account.id, str(user_id))).fetchone()
            if pending:
                from .access_requests import AccessRequestService
                AccessRequestService(local).decide(actor, pending['id'], 'approve', key)
            elif created or not ProfileRepository.has_vpn_profile(conn, account.id):
                # The person gains VPN access now; their bot menu follows.
                conn.execute("UPDATE backend_accounts SET revision=revision+1 WHERE id=?", (account.id,))
                revision = conn.execute('SELECT revision FROM backend_accounts WHERE id=?', (account.id,)).fetchone()['revision']
                conn.execute("""INSERT INTO backend_account_menu_refresh(account_id,revision) VALUES (?,?)
                    ON CONFLICT(account_id) DO UPDATE SET revision=excluded.revision""", (account.id, revision))
            conn.execute('DELETE FROM backend_telegram_menu_refresh WHERE subject=?', (str(user_id),))
            profile_id = ProfileRepository.ensure_account_profile_in_transaction(conn, account.id)
            profile = conn.execute('SELECT * FROM backend_profiles WHERE id=?', (profile_id,)).fetchone()
            result = {'account': self.public(self._read(conn, account.id)), 'profile': ProfileRepository.public(profile), 'created': created}
            conn.execute('INSERT INTO backend_account_commands VALUES (?,?,?,?,?)', (actor.account.id, key, account.id, input_json, json.dumps(result, sort_keys=True)))
            return result

    def update(self, actor, account_id, *, role=None, status=None, roles=None, revision, command_key):
        """Change an account's role or status, or (`roles`) assign its roles exactly."""
        require_permission(actor, 'accounts.manage')
        key = _key(command_key)
        if role is None and status is None and roles is None:
            raise AccessDenied('invalid_input', 422)
        if roles is not None:
            require_permission(actor, 'roles.manage')
            from .authorization import STAFF_ROLES
            if role is not None or not isinstance(roles, list) or set(roles) - STAFF_ROLES or len(set(roles)) != len(roles):
                raise AccessDenied('invalid_input', 422)
            roles = sorted(roles)
            role = 'admin' if roles else 'member'
        if role is not None and role not in {'member', 'admin'}:
            raise AccessDenied('invalid_input', 422)
        if status is not None and status not in {'approved', 'disabled'}:
            raise AccessDenied('invalid_input', 422)
        if type(revision) is not int or revision < 1:
            raise AccessDenied('invalid_revision', 422)
        input_json = json.dumps({'target': account_id, 'role': role, 'status': status,
                                 'revision': revision, **({'roles': roles} if roles is not None else {})}, sort_keys=True)
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            # One guard row serializes all admin removals across PostgreSQL workers.
            conn.execute('UPDATE backend_account_guard SET revision = revision + 1 WHERE id = 1')
            current_actor = conn.execute('SELECT role, status FROM backend_accounts WHERE id = ?',
                                         (actor.account.id,)).fetchone()
            if current_actor is None or current_actor['role'] != 'admin' or current_actor['status'] != 'approved':
                raise AccessDenied('permission_denied')
            previous = conn.execute('''SELECT target_account_id, input_json, result_json
                FROM backend_account_commands WHERE actor_account_id = ? AND command_key = ?''',
                (actor.account.id, key)).fetchone()
            if previous is not None:
                if previous['target_account_id'] != account_id or previous['input_json'] != input_json:
                    raise AccessDenied('idempotency_conflict', 409)
                return json.loads(previous['result_json'])
            row = self._read(conn, account_id)
            if row is None:
                raise AccessDenied('resource_not_found', 404)
            if conn.execute('SELECT 1 FROM backend_account_deletions WHERE account_id=?', (account_id,)).fetchone():
                raise AccessDenied('account_deleting', 409)
            if row['revision'] != revision:
                raise AccessDenied('revision_conflict', 412)
            next_role = role if role is not None else row['role']
            next_status = status if status is not None else row['status']
            current_roles = _roles(conn, row)
            roles_changed = roles is not None and roles != current_roles
            if roles_changed and 'owner' in current_roles and 'owner' not in roles:
                from .roles import owners
                if owners(conn) <= 1:
                    raise AccessDenied('last_owner_required', 409)
            if row['role'] == 'admin' and next_status != 'approved':
                raise AccessDenied('self_admin_revocation' if account_id == actor.account.id else 'admin_block_forbidden', 409)
            if roles_changed and (next_role, next_status) == (row['role'], row['status']):
                # Staff roles change; the account stays staff.
                from .roles import assign
                assign(conn, account_id, roles)
                conn.execute('UPDATE backend_accounts SET revision = revision + 1 WHERE id = ?', (account_id,))
                conn.execute("""INSERT INTO backend_account_menu_refresh(account_id,revision) VALUES (?,?)
                    ON CONFLICT(account_id) DO UPDATE SET revision=excluded.revision""", (account_id, revision + 1))
            if (next_role, next_status) != (row['role'], row['status']):
                if account_id == actor.account.id and (next_role, next_status) != ('admin', 'approved'):
                    raise AccessDenied('self_admin_revocation', 409)
                if row['role'] == 'admin' and row['status'] == 'approved' and (next_role, next_status) != ('admin', 'approved'):
                    remaining = conn.execute('''SELECT COUNT(*) AS count FROM backend_accounts
                        WHERE role = 'admin' AND status = 'approved' ''').fetchone()['count']
                    if remaining <= 1:
                        raise AccessDenied('last_admin_required', 409)
                    # Only an owner changes an owner's roles, and one owner stays.
                    if 'owner' in _roles(conn, row):
                        require_permission(actor, 'roles.manage')
                        from .roles import owners
                        if owners(conn) <= 1:
                            raise AccessDenied('last_owner_required', 409)
                pending = conn.execute('''SELECT id FROM backend_access_requests
                    WHERE account_id = ? AND status = 'pending' ''', (account_id,)).fetchone()
                if pending is not None:
                    raise AccessDenied('request_pending', 409)
                conn.execute('''UPDATE backend_accounts SET role = ?, status = ?, revision = revision + 1
                    WHERE id = ?''', (next_role, next_status, account_id))
                if next_role != row['role']:
                    # Promotion makes an administrator; demotion takes every role.
                    from .roles import assign
                    assign(conn, account_id, roles if roles is not None else {'admin'} if next_role == 'admin' else set())
                conn.execute("""INSERT INTO backend_account_menu_refresh(account_id,revision) VALUES (?,?)
                    ON CONFLICT(account_id) DO UPDATE SET revision=excluded.revision""", (account_id, revision + 1))
            from .profiles import ProfileRepository
            ProfileRepository.ensure_account_profile_in_transaction(conn, account_id)
            if next_role == 'admin':
                from .profile_commands import ProfileCommands
                ProfileCommands.restore_admin_expiries(conn, actor, account_id)
            elif row['role'] == 'admin':
                from .grant_policies import reconcile
                reconcile(conn, actor)
            result = self.public(self._read(conn, account_id))
            conn.execute('''INSERT INTO backend_account_commands
                (actor_account_id, command_key, target_account_id, input_json, result_json)
                VALUES (?, ?, ?, ?, ?)''',
                (actor.account.id, key, account_id, input_json, json.dumps(result, sort_keys=True)))
            return result
