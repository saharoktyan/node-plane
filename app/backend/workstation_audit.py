"""Workstation attribution and secret-free events; root SSH is the trust boundary."""
from datetime import datetime, timezone
import re
from uuid import uuid4


class WorkstationAudit:
    def __init__(self, db):
        self.db = db

    def initialize_schema(self):
        with self.db.transaction() as conn:
            # No FK to accounts/credentials: deletion or restoration must not erase history.
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_workstation_context (
                credential_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                account_id TEXT NOT NULL, account_label TEXT NOT NULL,
                ssh_user TEXT NOT NULL, device_fingerprint TEXT NOT NULL,
                created_at TEXT NOT NULL)''')
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_workstation_audit (
                id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL,
                credential_id TEXT, session_id TEXT NOT NULL,
                account_id TEXT NOT NULL, account_label TEXT NOT NULL,
                ssh_user TEXT NOT NULL, device_fingerprint TEXT NOT NULL,
                action TEXT NOT NULL, phase TEXT NOT NULL,
                request_id TEXT, command_id TEXT, http_status INTEGER)''')
            conn.execute('CREATE INDEX IF NOT EXISTS backend_workstation_audit_time ON backend_workstation_audit(occurred_at,id)')
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_workstation_enrollment (
                event_id TEXT PRIMARY KEY, target TEXT NOT NULL,
                key_fingerprint TEXT NOT NULL, outcome TEXT NOT NULL)''')

    @staticmethod
    def validate_context(context):
        if not isinstance(context, dict) or context.keys() != {'ssh_user', 'device_fingerprint'}:
            raise ValueError('invalid workstation attribution')
        if not isinstance(context['ssh_user'], str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', context['ssh_user']):
            raise ValueError('invalid SSH user')
        if not isinstance(context['device_fingerprint'], str) or not re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', context['device_fingerprint']):
            raise ValueError('invalid device fingerprint')
        return context

    def bind(self, credential_id, session_id, account_id, label, context):
        self.validate_context(context)
        label = ''.join(c for c in label if c.isprintable())[:200]
        with self.db.transaction() as conn:
            existing = conn.execute('SELECT * FROM backend_workstation_context WHERE credential_id=?', (credential_id,)).fetchone()
            if existing:
                if any(existing[k] != v for k, v in {'session_id': session_id, 'account_id': account_id, **context}.items()):
                    raise ValueError('workstation attribution conflict')
                return False
            conn.execute('INSERT INTO backend_workstation_context VALUES (?,?,?,?,?,?,?)',
                (credential_id, session_id, account_id, label, context['ssh_user'], context['device_fingerprint'], datetime.now(timezone.utc).isoformat()))
        return True

    def context(self, credential_id):
        with self.db.connect() as conn:
            row = conn.execute('SELECT * FROM backend_workstation_context WHERE credential_id=?', (credential_id,)).fetchone()
        return dict(row) if row else None

    def record(self, context, action, phase, *, request_id=None, command_id=None, http_status=None, enrollment=None):
        # No free-form descriptions, bodies, errors, query strings or credential secrets.
        if len(action) > 300 or not re.fullmatch(r'[A-Za-z0-9 /_.{}:-]+', action):
            raise ValueError('invalid audit action')
        if phase not in {'issued', 'revoked', 'admitted', 'completed'}:
            raise ValueError('invalid audit phase')
        event_id = str(uuid4())
        with self.db.transaction() as conn:
            conn.execute('INSERT INTO backend_workstation_audit VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (event_id, datetime.now(timezone.utc).isoformat(), context['credential_id'],
                 context['session_id'], context['account_id'], context['account_label'],
                 context['ssh_user'], context['device_fingerprint'], action, phase,
                 request_id, command_id, http_status))
            if enrollment:
                conn.execute('INSERT INTO backend_workstation_enrollment VALUES (?,?,?,?)',
                    (event_id, *enrollment))
        return event_id

    def record_enrollment(self, context, command_id, target, fingerprint, outcome):
        self.record(context, 'SSH controller key enrollment',
            'admitted' if outcome == 'admitted' else 'completed', command_id=command_id,
            enrollment=(target, fingerprint, outcome))

    def page(self, actor, offset=0):
        from .authorization import require_permission
        require_permission(actor, 'maintenance.manage')
        with self.db.connect() as conn:
            total = conn.execute('SELECT COUNT(*) AS count FROM backend_workstation_audit').fetchone()['count']
            rows = conn.execute('''SELECT occurred_at,session_id,account_id,account_label,
                ssh_user,device_fingerprint,action,phase,request_id,command_id,http_status
                ,e.target,e.key_fingerprint,e.outcome
                FROM backend_workstation_audit a LEFT JOIN backend_workstation_enrollment e ON e.event_id=a.id
                ORDER BY occurred_at DESC,a.id DESC LIMIT 10 OFFSET ?''', (offset,)).fetchall()
        return {'items': [dict(row) for row in rows], 'total': total, 'offset': offset, 'page_size': 10}
