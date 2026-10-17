"""Workstation attribution and secret-free events; root SSH is the trust boundary."""
from datetime import datetime, timezone
import re
from uuid import uuid4


class WorkstationAudit:
    def __init__(self, db):
        self.db = db

    def initialize_schema(self):
        with self.db.transaction() as conn:
            # Tombstones survive account deletion and database restoration.
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_workstation_keys (
                fingerprint TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                registered_at TEXT NOT NULL, revoked_at TEXT)''')
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

            conn.execute("""CREATE TABLE IF NOT EXISTS backend_workstation_audit_details (
                event_id TEXT PRIMARY KEY REFERENCES backend_workstation_audit(id) ON DELETE CASCADE,
                title TEXT NOT NULL)""")

    @staticmethod
    def describe(conn, action):
        if ' /api/v1/' not in action:
            return action
        method, path = action.split(' ', 1)
        parts = path.removeprefix('/api/v1/').split('/')
        resource = parts[0]
        verbs = {'POST': 'Create', 'PATCH': 'Update', 'PUT': 'Update', 'DELETE': 'Delete'}
        target = None
        if resource in {'profiles', 'accounts'} and len(parts) == 1:
            title = verbs.get(method, 'Manage') + (' profile' if resource == 'profiles' else ' account')
        elif resource in {'profiles', 'accounts'} and len(parts) > 1:
            account_id = parts[1]
            if resource == 'profiles':
                row = conn.execute('SELECT owner_account_id FROM backend_profiles WHERE id=?', (parts[1],)).fetchone()
                account_id = row['owner_account_id'] if row and row['owner_account_id'] else parts[1]
            identity = conn.execute("""SELECT i.subject,d.username FROM backend_external_identities i
                LEFT JOIN backend_telegram_identity_details d ON d.subject=i.subject
                WHERE i.account_id=? AND i.provider='telegram' ORDER BY i.subject LIMIT 1""", (account_id,)).fetchone()
            if not identity:
                import json
                archived = conn.execute('SELECT telegram_json FROM backend_account_deletions WHERE account_id=?', (account_id,)).fetchone()
                if archived:
                    saved = json.loads(archived['telegram_json'])
                    identity = {'username':saved.get('username'),'subject':saved.get('telegram_user_id')}
            target = '@' + identity['username'].lstrip('@') if identity and identity['username'] else identity['subject'] if identity and identity['subject'] else account_id
            name = 'profile' if resource == 'profiles' else 'account'
            if len(parts) > 2:
                names = {'grants': 'Change profile access', 'access-policy': 'Change profile access policy', 'devices': 'Manage profile devices', 'name': 'Rename profile', 'freeze': 'Freeze profile', 'unfreeze': 'Unfreeze profile', 'block': 'Block account', 'unblock': 'Unblock account', 'role': 'Change administrator rights', 'permissions': 'Change administrator rights'}
                title = names.get(parts[2], 'Manage profile ' + parts[2].replace('-', ' '))
            else:
                title = verbs.get(method, 'Manage') + ' ' + name
        elif resource == 'nodes':
            title = verbs.get(method, 'Manage') + ' server'
            if len(parts) > 2:
                title = {'settings': 'Change server settings', 'jobs': 'Run server maintenance', 'removal': 'Remove server', 'bootstrap': 'Set up server'}.get(parts[2], 'Manage server ' + parts[2].replace('-', ' '))
            if len(parts) > 1:
                row = conn.execute('SELECT title FROM backend_nodes WHERE key=?', (parts[1],)).fetchone()
                target = row['title'] if row else parts[1]
        else:
            titles = {'system/bot-title': 'Change bot name', 'system/backups/jobs': 'Run backup operation',
                'system/backups/preferences': 'Change automatic backup settings', 'system/traffic/preferences': 'Change traffic collection settings',
                'announcements': 'Send announcement', 'announcements/preview': 'Preview announcement',
                'system/installation-defaults': 'Change server installation defaults',
                'system/alerts/preferences': 'Change alert settings', 'system/alerts/refresh': 'Refresh alert status', 'system/telegram/token': 'Change bot token',
                'system/updates/run':'Run controller update', 'system/updates/check':'Check controller updates'}
            suffix = path.removeprefix('/api/v1/')
            categories = {'system':'controller settings','access-requests':'access request','devices':'device','temporary-configs':'temporary configuration','operations':'operation','announcements':'announcement','integrations':'integration','me':'account settings','onboarding':'Quick Start'}
            title = titles.get(suffix, verbs.get(method, 'Manage') + ' ' + categories.get(resource,'controller'))
        title = title + (' · ' + str(target) if target else '')
        return ''.join(c for c in title if c.isprintable())[:400]

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
            title = self.describe(conn, action)
            if phase == 'completed' and request_id:
                previous = conn.execute("""SELECT d.title FROM backend_workstation_audit_details d
                    JOIN backend_workstation_audit a ON a.id=d.event_id
                    WHERE a.request_id=? AND a.phase='admitted' LIMIT 1""", (request_id,)).fetchone()
                if previous:
                    title = previous['title']
            conn.execute('INSERT INTO backend_workstation_audit_details(event_id,title) VALUES (?,?)', (event_id, title))
            if enrollment:
                conn.execute('INSERT INTO backend_workstation_enrollment VALUES (?,?,?,?)',
                    (event_id, *enrollment))
        return event_id

    def record_enrollment(self, context, command_id, target, fingerprint, outcome):
        self.record(context, 'SSH controller key enrollment',
            'admitted' if outcome == 'admitted' else 'completed', command_id=command_id,
            enrollment=(target, fingerprint, outcome))

    def page(self, actor, offset=0, errors_only=False):
        from .authorization import require_permission
        require_permission(actor, 'maintenance.manage')
        joins = ' FROM backend_workstation_audit a LEFT JOIN backend_workstation_enrollment e ON e.event_id=a.id LEFT JOIN backend_workstation_audit_details d ON d.event_id=a.id '
        where = "WHERE a.http_status >= 400 OR e.outcome = 'unconfirmed'" if errors_only else ''
        with self.db.connect() as conn:
            total = conn.execute('SELECT COUNT(*) AS count' + joins + where).fetchone()['count']
            offset = min(offset, max(0, (total - 1) // 10 * 10))
            rows = conn.execute('SELECT occurred_at,session_id,account_id,account_label, '
                'ssh_user,device_fingerprint,action,phase,request_id,command_id,http_status, '
                'e.target,e.key_fingerprint,e.outcome,d.title' + joins + where +
                ' ORDER BY occurred_at DESC,a.id DESC LIMIT 10 OFFSET ?', (offset,)).fetchall()
            items = [dict(row) for row in rows]
            for item in items:
                if not item['title']:
                    item['title'] = self.describe(conn, item['action'])
        return {'items': items, 'total': total, 'offset': offset, 'page_size': 10, 'errors_only': errors_only}
