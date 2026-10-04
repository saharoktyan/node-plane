"""Administrator account reads and guarded account-state changes."""
from __future__ import annotations

import json
from uuid import UUID

from .authorization import AccessDenied, require_permission
from .profiles import _cursor, _page


def _key(value):
    try:
        return str(UUID(value))
    except (TypeError, ValueError, AttributeError):
        raise AccessDenied('invalid_idempotency_key', 422) from None


class AccountService:
    def __init__(self, db):
        self.db = db

    def initialize_schema(self):
        with self.db.transaction() as conn:
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
        for key in ('username', 'first_name', 'last_name', 'language_code', 'locale', 'locale_selected'):
            if key in row.keys():
                result[key] = bool(row[key]) if key == 'locale_selected' else row[key]
        return result

    @staticmethod
    def _read(conn, account_id):
        return conn.execute('''SELECT a.id, a.role, a.status, a.revision,
            i.subject AS telegram_subject, d.username, d.first_name, d.last_name,
            d.language_code, d.locale, d.locale_selected
            FROM backend_accounts a
            LEFT JOIN backend_external_identities i ON i.account_id = a.id AND i.provider = 'telegram'
            LEFT JOIN backend_telegram_identity_details d ON d.subject = i.subject
            WHERE a.id = ?''', (account_id,)).fetchone()

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
                i.subject AS telegram_subject, d.username, d.first_name, d.last_name,
                d.language_code, d.locale, d.locale_selected
                FROM backend_accounts a
                LEFT JOIN backend_external_identities i ON i.account_id = a.id AND i.provider = 'telegram'
                LEFT JOIN backend_telegram_identity_details d ON d.subject = i.subject
                WHERE a.id > ? ORDER BY a.id LIMIT ?''', (after, limit + 1)).fetchall()
        return _page([self.public(row) for row in rows], limit, 'accounts', 'id')

    def update(self, actor, account_id, *, role=None, status=None, revision, command_key):
        require_permission(actor, 'accounts.manage')
        key = _key(command_key)
        if role is None and status is None:
            raise AccessDenied('invalid_input', 422)
        if role is not None and role not in {'member', 'admin'}:
            raise AccessDenied('invalid_input', 422)
        if status is not None and status not in {'pending', 'approved', 'rejected', 'disabled'}:
            raise AccessDenied('invalid_input', 422)
        if type(revision) is not int or revision < 1:
            raise AccessDenied('invalid_revision', 422)
        input_json = json.dumps({'target': account_id, 'role': role, 'status': status,
                                 'revision': revision}, sort_keys=True)
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
            if row['revision'] != revision:
                raise AccessDenied('revision_conflict', 412)
            next_role = role if role is not None else row['role']
            next_status = status if status is not None else row['status']
            if (next_role, next_status) != (row['role'], row['status']):
                if account_id == actor.account.id and (next_role, next_status) != ('admin', 'approved'):
                    raise AccessDenied('self_admin_revocation', 409)
                if row['role'] == 'admin' and row['status'] == 'approved' and (next_role, next_status) != ('admin', 'approved'):
                    remaining = conn.execute('''SELECT COUNT(*) AS count FROM backend_accounts
                        WHERE role = 'admin' AND status = 'approved' ''').fetchone()['count']
                    if remaining <= 1:
                        raise AccessDenied('last_admin_required', 409)
                pending = conn.execute('''SELECT id FROM backend_access_requests
                    WHERE account_id = ? AND status = 'pending' ''', (account_id,)).fetchone()
                if pending is not None:
                    raise AccessDenied('request_pending', 409)
                conn.execute('''UPDATE backend_accounts SET role = ?, status = ?, revision = revision + 1
                    WHERE id = ?''', (next_role, next_status, account_id))
            if next_role == 'admin':
                from .profile_commands import ProfileCommands
                ProfileCommands.restore_admin_expiries(conn, actor, account_id)
            result = self.public(self._read(conn, account_id))
            conn.execute('''INSERT INTO backend_account_commands
                (actor_account_id, command_key, target_account_id, input_json, result_json)
                VALUES (?, ?, ?, ?, ?)''',
                (actor.account.id, key, account_id, input_json, json.dumps(result, sort_keys=True)))
            return result
