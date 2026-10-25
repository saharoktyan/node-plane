"""Account access requests, independent of Telegram delivery and VPN grants."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from uuid import UUID, uuid4

from .authorization import AccessDenied, require_permission
from .profiles import _cursor, _page
from .telegram_visitors import TelegramVisitor


def _command_key(value: str) -> str:
    try:
        return str(UUID(value))
    except (TypeError, ValueError, AttributeError):
        raise AccessDenied('invalid_idempotency_key', 422) from None


class AccessRequestService:
    def __init__(self, db):
        self.db = db

    def initialize_schema(self):
        with self.db.transaction() as conn:
            from db.migration_revisions.r0007_account_menu_refresh import upgrade
            upgrade(conn)
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_system_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )''')
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_access_requests (
                id TEXT PRIMARY KEY,
                account_id TEXT REFERENCES backend_accounts(id),
                telegram_subject TEXT REFERENCES backend_telegram_identity_details(subject),
                create_key TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'rejected')),
                created_at TEXT NOT NULL,
                decided_at TEXT,
                decided_by TEXT REFERENCES backend_accounts(id),
                decision_key TEXT,
                UNIQUE(account_id, create_key),
                CHECK(account_id IS NOT NULL OR telegram_subject IS NOT NULL)
            )''')
            conn.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_backend_pending_access_request
                ON backend_access_requests(account_id) WHERE status = 'pending' ''')
            conn.execute('''CREATE INDEX IF NOT EXISTS idx_backend_access_requests_status
                ON backend_access_requests(status, id)''')
            conn.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_backend_guest_request_key
                ON backend_access_requests(telegram_subject,create_key) WHERE telegram_subject IS NOT NULL''')
            conn.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_backend_pending_guest_request
                ON backend_access_requests(telegram_subject) WHERE status='pending' AND account_id IS NULL''')
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_telegram_menu_refresh (
                subject TEXT PRIMARY KEY REFERENCES backend_telegram_identity_details(subject),
                revision INTEGER NOT NULL)''')
            from db.migration_revisions.r0008_access_decision_delivery import upgrade
            upgrade(conn)

    @staticmethod
    def _policy(conn):
        rows = {row['key']: row['value'] for row in conn.execute('''SELECT key, value
            FROM backend_system_settings WHERE key IN ('access_requests_enabled', 'access_gate_message', 'access_welcome_message')''')}
        return {
            'enabled': rows.get('access_requests_enabled', '1') == '1',
            'gate_message': rows.get('access_gate_message', 'Authorization is required to use this bot.'),
            'welcome_message': rows.get('access_welcome_message', 'Request access to use this bot.'),
        }

    def policy(self, actor):
        if isinstance(actor, TelegramVisitor):
            actor.require('access_requests.self.read')
            with self.db.connect() as conn:
                return {**self._policy(conn), 'notify_requests': None}
        require_permission(actor, 'access_requests.self.read')
        with self.db.connect() as conn:
            result = self._policy(conn)
            if actor.account.role == 'admin' and actor.account.status == 'approved':
                key = f"admin_request_notifications:{actor.account.id}"
                row = conn.execute('SELECT value FROM backend_system_settings WHERE key = ?', (key,)).fetchone()
                result['notify_requests'] = not row or row['value'] != '0'
            else:
                result['notify_requests'] = None
            return result

    def update_policy(self, actor, *, enabled=None, gate_message=None, welcome_message=None, notify_requests=None):
        require_permission(actor, 'settings.manage')
        if enabled is None and gate_message is None and welcome_message is None and notify_requests is None:
            raise AccessDenied('invalid_input', 422)
        if any(value is not None and type(value) is not bool
               for value in (enabled, notify_requests)):
            raise AccessDenied('invalid_input', 422)
        for message in (gate_message, welcome_message):
            if message is not None and (not isinstance(message, str) or
                                       not message.strip() or len(message.strip()) > 500):
                raise AccessDenied('invalid_input', 422)
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            if enabled is not None:
                conn.execute('''INSERT INTO backend_system_settings(key, value) VALUES ('access_requests_enabled', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value''', ('1' if enabled else '0',))
            if gate_message is not None:
                conn.execute('''INSERT INTO backend_system_settings(key, value) VALUES ('access_gate_message', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value''', (gate_message.strip(),))
            if welcome_message is not None:
                conn.execute('''INSERT INTO backend_system_settings(key, value) VALUES ('access_welcome_message', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value''', (welcome_message.strip(),))
            if notify_requests is not None:
                key = f"admin_request_notifications:{actor.account.id}"
                conn.execute('''INSERT INTO backend_system_settings(key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value''',
                    (key, '1' if notify_requests else '0'))
            result = self._policy(conn)
            key = f"admin_request_notifications:{actor.account.id}"
            row = conn.execute('SELECT value FROM backend_system_settings WHERE key = ?', (key,)).fetchone()
            result['notify_requests'] = not row or row['value'] != '0'
            return result

    @staticmethod
    def public(row):
        result = {key: row[key] for key in ('id', 'account_id', 'status', 'created_at', 'decided_at', 'decided_by')}
        for key in ('telegram_user_id', 'username', 'first_name', 'last_name', 'language_code', 'locale'):
            if key in row.keys():
                result[key] = row[key]
        if result.get('telegram_user_id') is not None:
            result['telegram_user_id'] = int(result['telegram_user_id'])
        return result

    @staticmethod
    def _pending_query():
        return '''SELECT r.*, COALESCE(r.telegram_subject,i.subject) AS telegram_user_id,
            d.username, d.first_name, d.last_name, d.language_code, d.locale
            FROM backend_access_requests r
            LEFT JOIN backend_external_identities i ON i.account_id = r.account_id AND i.provider = 'telegram'
            LEFT JOIN backend_telegram_identity_details d ON d.subject = COALESCE(r.telegram_subject,i.subject)'''

    def create(self, actor, command_key):
        if isinstance(actor, TelegramVisitor):
            return self._create_visitor(actor, command_key)
        require_permission(actor, 'access_requests.self.create')
        key = _command_key(command_key)
        account_id = actor.account.id
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            # Serialize requests and decisions for this account on PostgreSQL.
            conn.execute('UPDATE backend_accounts SET status = status WHERE id = ?', (account_id,))
            previous = conn.execute('''SELECT * FROM backend_access_requests
                WHERE account_id = ? AND create_key = ?''', (account_id, key)).fetchone()
            if previous is not None:
                return self.public(previous)
            if not self._policy(conn)['enabled']:
                raise AccessDenied('access_requests_disabled', 403)
            account = conn.execute('SELECT status FROM backend_accounts WHERE id = ?', (account_id,)).fetchone()
            if account is None or account['status'] not in {'pending', 'rejected'}:
                raise AccessDenied('request_not_allowed', 409)
            pending = conn.execute('''SELECT id FROM backend_access_requests
                WHERE account_id = ? AND status = 'pending' ''', (account_id,)).fetchone()
            if pending is not None:
                raise AccessDenied('request_already_pending', 409)
            request_id = str(uuid4())
            now = datetime.now(timezone.utc).isoformat()
            conn.execute('''INSERT INTO backend_access_requests
                (id, account_id, create_key, status, created_at) VALUES (?, ?, ?, 'pending', ?)''',
                (request_id, account_id, key, now))
            row = conn.execute('SELECT * FROM backend_access_requests WHERE id = ?', (request_id,)).fetchone()
            return self.public(row)

    def list_own(self, actor, *, limit=25, cursor=None):
        if isinstance(actor, TelegramVisitor):
            actor.require('access_requests.self.read')
            after = _cursor(cursor, 'access_requests_self')
            with self.db.connect() as conn:
                rows = conn.execute(self._pending_query() + '''
                    WHERE r.telegram_subject=? AND r.account_id IS NULL AND r.id>? ORDER BY r.id LIMIT ?''',
                    (actor.subject, after, limit + 1)).fetchall()
            return _page([self.public(row) for row in rows], limit, 'access_requests_self', 'id')
        require_permission(actor, 'access_requests.self.read')
        after = _cursor(cursor, 'access_requests_self')
        with self.db.connect() as conn:
            rows = conn.execute(self._pending_query() + ''' WHERE r.account_id = ?
                AND r.id > ? ORDER BY r.id LIMIT ?''', (actor.account.id, after, limit + 1)).fetchall()
        return _page([self.public(row) for row in rows], limit, 'access_requests_self', 'id')

    def list_pending(self, actor, *, limit=25, cursor=None, search=None):
        require_permission(actor, 'access_requests.manage')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AccessDenied('invalid_input', 422)
        if search is not None and (not isinstance(search, str) or
                                   not 1 <= len(search.strip()) <= 128):
            raise AccessDenied('invalid_input', 422)
        term = search.strip().casefold() if search else None
        kind = 'access_requests_pending:' + term if term else 'access_requests_pending'
        after = _cursor(cursor, kind)
        with self.db.connect() as conn:
            pending_total = conn.execute("SELECT COUNT(*) AS count FROM backend_access_requests WHERE status = 'pending'").fetchone()['count']
            if term is None:
                rows = conn.execute(self._pending_query() + ''' WHERE r.status = 'pending'
                    AND r.id > ? ORDER BY r.id LIMIT ?''', (after, limit + 1)).fetchall()
                matches = [self.public(row) for row in rows]
            else:
                matches, current = [], after
                while len(matches) < limit + 1:
                    rows = conn.execute(self._pending_query() + ''' WHERE r.status = 'pending'
                        AND r.id > ? ORDER BY r.id LIMIT 200''', (current,)).fetchall()
                    if not rows:
                        break
                    for row in rows:
                        current = row['id']
                        item = self.public(row)
                        haystack = ' '.join(str(item.get(field) or '') for field in
                            ('telegram_user_id', 'username', 'first_name', 'last_name', 'account_id')).casefold()
                        if term in haystack:
                            matches.append(item)
                            if len(matches) == limit + 1:
                                break
                    if len(rows) < 200:
                        break
        return {**_page(matches, limit, kind, 'id'), 'pending_total': pending_total}

    def get_pending(self, actor, request_id):
        require_permission(actor, 'access_requests.manage')
        with self.db.connect() as conn:
            row = conn.execute(self._pending_query() +
                " WHERE r.id = ? AND r.status = 'pending'", (request_id,)).fetchone()
        if row is None:
            raise AccessDenied('resource_not_found', 404)
        return self.public(row)

    def _create_visitor(self, visitor, command_key):
        visitor.require('access_requests.self.create')
        key = _command_key(command_key)
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            conn.execute('UPDATE backend_account_guard SET revision=revision WHERE id=1')
            conn.execute('UPDATE backend_telegram_identity_details SET updated_at=updated_at WHERE subject=?', (visitor.subject,))
            linked = conn.execute("SELECT account_id FROM backend_external_identities WHERE provider='telegram' AND subject=?", (visitor.subject,)).fetchone()
            if linked:
                raise AccessDenied('request_not_allowed', 409)
            previous = conn.execute('SELECT * FROM backend_access_requests WHERE telegram_subject=? AND create_key=?',
                (visitor.subject, key)).fetchone()
            if previous:
                return self.public(previous)
            if not self._policy(conn)['enabled']:
                raise AccessDenied('access_requests_disabled', 403)
            pending = conn.execute("SELECT id FROM backend_access_requests WHERE telegram_subject=? AND account_id IS NULL AND status='pending'", (visitor.subject,)).fetchone()
            if pending:
                raise AccessDenied('request_already_pending', 409)
            identity = str(uuid4())
            conn.execute("""INSERT INTO backend_access_requests
                (id,telegram_subject,create_key,status,created_at) VALUES (?,?,?,'pending',?)""",
                (identity, visitor.subject, key, datetime.now(timezone.utc).isoformat()))
            return self.public(conn.execute('SELECT * FROM backend_access_requests WHERE id=?', (identity,)).fetchone())

    def decide(self, actor, request_id, decision, command_key):
        require_permission(actor, 'access_requests.manage')
        key = _command_key(command_key)
        if decision not in {'approve', 'reject'}:
            raise AccessDenied('invalid_input', 422)
        status = 'approved' if decision == 'approve' else 'rejected'
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            conn.execute('UPDATE backend_account_guard SET revision=revision WHERE id=1')
            administrator = conn.execute('SELECT role,status FROM backend_accounts WHERE id=?', (actor.account.id,)).fetchone()
            if not administrator or administrator['role'] != 'admin' or administrator['status'] != 'approved':
                raise AccessDenied('permission_denied')
            row = conn.execute('SELECT * FROM backend_access_requests WHERE id = ?', (request_id,)).fetchone()
            if row is None:
                raise AccessDenied('resource_not_found', 404)
            if row['status'] != 'pending':
                if row['status'] == status and row['decision_key'] == key and row['decided_by'] == actor.account.id:
                    return self.public(conn.execute(self._pending_query() + ' WHERE r.id=?', (request_id,)).fetchone())
                raise AccessDenied('request_already_decided', 409)
            account_id, subject = row['account_id'], row['telegram_subject']
            if account_id:
                conn.execute('UPDATE backend_accounts SET status=status WHERE id=?', (account_id,))
                account = conn.execute('SELECT status FROM backend_accounts WHERE id=?', (account_id,)).fetchone()
                if account is None or account['status'] not in {'pending', 'rejected'}:
                    raise AccessDenied('request_not_allowed', 409)
            elif not subject:
                raise AccessDenied('invalid_request_state', 409)
            else:
                conn.execute('UPDATE backend_telegram_identity_details SET updated_at=updated_at WHERE subject=?', (subject,))
                if decision == 'approve':
                    linked = conn.execute("SELECT a.id,a.status FROM backend_accounts a JOIN backend_external_identities i ON i.account_id=a.id WHERE i.provider='telegram' AND i.subject=?", (subject,)).fetchone()
                    if linked and linked['status'] == 'disabled':
                        raise AccessDenied('account_disabled', 409)
                    account_id = linked['id'] if linked else str(uuid4())
                    if not linked:
                        conn.execute("INSERT INTO backend_accounts(id,status) VALUES (?,'approved')", (account_id,))
                        conn.execute("INSERT INTO backend_external_identities(provider,subject,account_id) VALUES ('telegram',?,?)", (subject, account_id))
            now = datetime.now(timezone.utc).isoformat()
            conn.execute('''UPDATE backend_access_requests SET status = ?, account_id = ?, decided_at = ?,
                decided_by = ?, decision_key = ? WHERE id = ?''',
                (status, account_id, now, actor.account.id, key, request_id))
            if account_id:
                conn.execute('UPDATE backend_accounts SET status=?,revision=revision+1 WHERE id=?', (status, account_id))
                revision = conn.execute('SELECT revision FROM backend_accounts WHERE id=?', (account_id,)).fetchone()['revision']
                conn.execute('INSERT INTO backend_account_menu_refresh(account_id,revision) VALUES (?,?) ON CONFLICT(account_id) DO UPDATE SET revision=excluded.revision', (account_id, revision))
                if decision == 'approve':
                    from .profiles import ProfileRepository
                    ProfileRepository.ensure_account_profile_in_transaction(conn, account_id)
                    if subject:
                        conn.execute('DELETE FROM backend_telegram_menu_refresh WHERE subject=?', (subject,))
            else:
                conn.execute('''INSERT INTO backend_telegram_menu_refresh(subject,revision) VALUES (?,1)
                    ON CONFLICT(subject) DO UPDATE SET revision=backend_telegram_menu_refresh.revision+1''', (subject,))
            from .accounts import AccountService
            from .authorization import PrincipalKind
            decider = AccountService.public(AccountService._read(conn, actor.account.id))
            conn.execute('INSERT INTO backend_access_decision_details(request_id,decider_json,surface) VALUES (?,?,?)',
                (request_id, json.dumps(decider), 'workstation' if actor.principal.kind == PrincipalKind.ACCOUNT else 'telegram'))
            conn.execute("UPDATE backend_access_request_messages SET status='queued' WHERE request_id=? AND status='pending'", (request_id,))
            result = conn.execute(self._pending_query() + ' WHERE r.id=?', (request_id,)).fetchone()
            return self.public(result)
