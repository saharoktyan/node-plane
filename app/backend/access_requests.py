"""Account access requests, independent of Telegram delivery and VPN grants."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from .authorization import AccessDenied, require_permission
from .profiles import _cursor, _page


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
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_access_requests (
                id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL REFERENCES backend_accounts(id),
                create_key TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'rejected')),
                created_at TEXT NOT NULL,
                decided_at TEXT,
                decided_by TEXT REFERENCES backend_accounts(id),
                decision_key TEXT,
                UNIQUE(account_id, create_key)
            )''')
            conn.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_backend_pending_access_request
                ON backend_access_requests(account_id) WHERE status = 'pending' ''')
            conn.execute('''CREATE INDEX IF NOT EXISTS idx_backend_access_requests_status
                ON backend_access_requests(status, id)''')

    @staticmethod
    def public(row):
        return {key: row[key] for key in ('id', 'account_id', 'status', 'created_at', 'decided_at', 'decided_by')}

    def create(self, actor, command_key):
        require_permission(actor, 'access_requests.self.create')
        key = _command_key(command_key)
        account_id = actor.account.id
        with self.db.transaction() as conn:
            # Serialize requests and decisions for this account on PostgreSQL.
            conn.execute('UPDATE backend_accounts SET status = status WHERE id = ?', (account_id,))
            previous = conn.execute('''SELECT * FROM backend_access_requests
                WHERE account_id = ? AND create_key = ?''', (account_id, key)).fetchone()
            if previous is not None:
                return self.public(previous)
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
        require_permission(actor, 'access_requests.self.read')
        after = _cursor(cursor, 'access_requests_self')
        with self.db.connect() as conn:
            rows = conn.execute('''SELECT * FROM backend_access_requests WHERE account_id = ?
                AND id > ? ORDER BY id LIMIT ?''', (actor.account.id, after, limit + 1)).fetchall()
        return _page([self.public(row) for row in rows], limit, 'access_requests_self', 'id')

    def list_pending(self, actor, *, limit=25, cursor=None):
        require_permission(actor, 'access_requests.manage')
        after = _cursor(cursor, 'access_requests_pending')
        with self.db.connect() as conn:
            rows = conn.execute('''SELECT * FROM backend_access_requests WHERE status = 'pending'
                AND id > ? ORDER BY id LIMIT ?''', (after, limit + 1)).fetchall()
        return _page([self.public(row) for row in rows], limit, 'access_requests_pending', 'id')

    def decide(self, actor, request_id, decision, command_key):
        require_permission(actor, 'access_requests.manage')
        key = _command_key(command_key)
        if decision not in {'approve', 'reject'}:
            raise AccessDenied('invalid_input', 422)
        status = 'approved' if decision == 'approve' else 'rejected'
        with self.db.transaction() as conn:
            row = conn.execute('SELECT * FROM backend_access_requests WHERE id = ?', (request_id,)).fetchone()
            if row is None:
                raise AccessDenied('resource_not_found', 404)
            conn.execute('UPDATE backend_accounts SET status = status WHERE id = ?', (row['account_id'],))
            row = conn.execute('SELECT * FROM backend_access_requests WHERE id = ?', (request_id,)).fetchone()
            if row['status'] != 'pending':
                if row['status'] == status and row['decision_key'] == key and row['decided_by'] == actor.account.id:
                    return self.public(row)
                raise AccessDenied('request_already_decided', 409)
            account = conn.execute('SELECT status FROM backend_accounts WHERE id = ?', (row['account_id'],)).fetchone()
            if account is None or account['status'] not in {'pending', 'rejected'}:
                raise AccessDenied('request_not_allowed', 409)
            now = datetime.now(timezone.utc).isoformat()
            conn.execute('''UPDATE backend_access_requests SET status = ?, decided_at = ?,
                decided_by = ?, decision_key = ? WHERE id = ?''',
                (status, now, actor.account.id, key, request_id))
            conn.execute('UPDATE backend_accounts SET status = ?, revision = revision + 1 WHERE id = ?',
                         (status, row['account_id']))
            result = conn.execute('SELECT * FROM backend_access_requests WHERE id = ?', (request_id,)).fetchone()
            return self.public(result)
