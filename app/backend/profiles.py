"""VPN profile/grant models and trusted local provisioning.

These tables are one service-specific slice. Future Matrix/password-manager
access must not be represented as another value in the VPN protocol column.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
from uuid import uuid4

from .authorization import AccessDenied, ProfileResource, require_permission, require_profile


def _cursor(value, kind):
    if not value:
        return ''
    try:
        data = json.loads(base64.b64decode(value, altchars=b'-_', validate=True))
        if data['kind'] != kind or not isinstance(data['after'], str):
            raise ValueError()
        return data['after']
    except (ValueError, KeyError, TypeError):
        raise AccessDenied('invalid_cursor', 422) from None


def _page(items, limit, kind, key):
    cursor = None
    if len(items) > limit:
        items = items[:limit]
        cursor = base64.urlsafe_b64encode(json.dumps({'kind': kind, 'after': items[-1][key]}).encode()).decode()
    return {'items': items, 'next_cursor': cursor}


class ProfileRepository:
    def __init__(self, db):
        self.db = db

    def ensure_account_profile(self, account_id):
        """Create the default VPN profile once, without replacing custom names."""
        with self.db.transaction() as conn:
            return self.ensure_account_profile_in_transaction(conn, account_id)

    @staticmethod
    def ensure_account_profile_in_transaction(conn, account_id):
        conn.execute('UPDATE backend_accounts SET revision = revision WHERE id = ?', (account_id,))
        account = conn.execute('''SELECT a.role, i.subject, d.username
            FROM backend_accounts a LEFT JOIN backend_external_identities i
            ON i.account_id = a.id AND i.provider = 'telegram'
            LEFT JOIN backend_telegram_identity_details d ON d.subject = i.subject
            WHERE a.id = ?''', (account_id,)).fetchone()
        if account is None:
            raise AccessDenied('resource_not_found', 404)
        fallback = f"{'Admin' if account['role'] == 'admin' else 'User'} {account['subject'] or account_id}"
        name = account['username'] or fallback
        existing = conn.execute('''SELECT p.id, p.display_name FROM backend_profiles p
            WHERE p.owner_account_id = ? AND NOT EXISTS
                (SELECT 1 FROM backend_profile_deletions d WHERE d.profile_id = p.id)
            ORDER BY p.id LIMIT 1''',
                                (account_id,)).fetchone()
        if existing:
            automatic_names = {f'Admin {account["subject"]}', f'User {account["subject"]}'}
            if account['username'] and existing['display_name'] in automatic_names:
                conn.execute('UPDATE backend_profiles SET display_name = ? WHERE id = ?', (name, existing['id']))
            return existing['id']
        profile_id = str(uuid4())
        conn.execute('''INSERT INTO backend_profiles
            (id, runtime_name, display_name, owner_account_id, created_at)
            VALUES (?, ?, ?, ?, ?)''', (profile_id, 'account_' + profile_id.replace('-', ''),
                                      name, account_id, datetime.now(timezone.utc).isoformat()))
        return profile_id

    @staticmethod
    def revoke_orphaned_members(conn, account_id=None):
        """Require approval again after a member's last profile is deleted."""
        owner_filter = ' AND id = ?' if account_id else ''
        conn.execute("""UPDATE backend_accounts SET status = 'pending', revision = revision + 1
            WHERE role = 'member' AND status = 'approved'
            AND EXISTS (SELECT 1 FROM backend_profiles p
                JOIN backend_profile_deletions d ON d.profile_id = p.id
                WHERE p.owner_account_id = backend_accounts.id)
            AND NOT EXISTS (SELECT 1 FROM backend_profiles p
                WHERE p.owner_account_id = backend_accounts.id AND NOT EXISTS
                    (SELECT 1 FROM backend_profile_deletions d WHERE d.profile_id = p.id))"""
            + owner_filter, (account_id,) if account_id else ())

    def initialize_schema(self):
        from .alerts import AlertService
        AlertService(self.db).initialize_schema()
        from .announcements import AnnouncementService
        AnnouncementService(self.db).initialize_schema()
        from .backups import BackupService
        BackupService(self.db).initialize_schema()
        with self.db.transaction() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_profiles (
                id TEXT PRIMARY KEY,
                runtime_name TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                owner_account_id TEXT REFERENCES backend_accounts(id),
                frozen INTEGER NOT NULL DEFAULT 0 CHECK(frozen IN (0, 1)),
                expires_at TEXT,
                created_at TEXT,
                desired_revision INTEGER NOT NULL DEFAULT 1
            )''')
            if getattr(self.db, 'backend_name', '') == 'postgres':
                conn.execute('ALTER TABLE backend_profiles ADD COLUMN IF NOT EXISTS created_at TEXT')
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_nodes (
                key TEXT PRIMARY KEY, title TEXT NOT NULL, region TEXT NOT NULL,
                flag TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
                protocols_json TEXT NOT NULL,
                xray_transports_json TEXT NOT NULL DEFAULT '[]',
                desired_revision INTEGER NOT NULL DEFAULT 1,
                applied_revision INTEGER NOT NULL DEFAULT 0,
                settings_json TEXT NOT NULL DEFAULT '{}'
            )''')
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_grants (
                profile_id TEXT NOT NULL REFERENCES backend_profiles(id) ON DELETE CASCADE,
                node_key TEXT NOT NULL REFERENCES backend_nodes(key) ON DELETE CASCADE,
                protocol TEXT NOT NULL CHECK(protocol IN ('awg', 'xray')),
                PRIMARY KEY(profile_id, node_key, protocol)
            )''')
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_profile_deletions (
                profile_id TEXT PRIMARY KEY REFERENCES backend_profiles(id),
                requested_at TEXT NOT NULL,
                operation_id TEXT
            )''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_backend_profile_owner ON backend_profiles(owner_account_id, id)')
            # Repair accounts left approved by older profile-deletion commands.
            self.revoke_orphaned_members(conn)
        from .node_lifecycle import NodeLifecycle
        NodeLifecycle(self.db).initialize_schema()
        from .node_operations import NodeOperations
        NodeOperations(self.db).initialize_schema()
        from .node_removal import NodeRemovalService
        NodeRemovalService(self.db).initialize_schema()
        from .system_settings import SystemSettingsService
        SystemSettingsService(self.db).initialize_schema()
        from .traffic import TrafficService
        TrafficService(self.db).initialize_schema()
        from .system_cleanup import SystemCleanupService
        SystemCleanupService(self.db).initialize_schema()

    def create_profile(self, *, runtime_name, display_name, owner_account_id=None):
        # Trusted local provisioning only, not an unauthenticated HTTP route.
        if not isinstance(runtime_name, str) or not runtime_name or len(runtime_name) > 64 or not all(c.isascii() and (c.isalnum() or c in '_-') for c in runtime_name):
            raise ValueError('invalid runtime_name')
        if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 128:
            raise ValueError('invalid display_name')
        profile_id = str(uuid4())
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            if owner_account_id is not None and conn.execute('SELECT id FROM backend_accounts WHERE id = ?', (owner_account_id,)).fetchone() is None:
                raise ValueError('owner account does not exist')
            conn.execute('''INSERT INTO backend_profiles
                (id, runtime_name, display_name, owner_account_id, created_at)
                VALUES (?, ?, ?, ?, ?)''',
                (profile_id, runtime_name, display_name, owner_account_id,
                 datetime.now(timezone.utc).isoformat()))
        return profile_id

    @staticmethod
    def public(row):
        return {key: row[key] for key in ('id', 'display_name', 'owner_account_id', 'expires_at', 'desired_revision')} | {
            'frozen': bool(row['frozen']),
            'deleting': bool(row['deleting']) if 'deleting' in row.keys() else False}

    def get(self, profile_id):
        with self.db.connect() as conn:
            return conn.execute('''SELECT p.*, CASE WHEN d.profile_id IS NULL THEN 0 ELSE 1 END AS deleting
                FROM backend_profiles p LEFT JOIN backend_profile_deletions d ON d.profile_id = p.id
                WHERE p.id = ?''', (profile_id,)).fetchone()

    def owned(self, account_id, *, after, limit):
        with self.db.connect() as conn:
            rows = conn.execute('''SELECT p.* FROM backend_profiles p WHERE p.owner_account_id = ? AND p.id > ?
                AND NOT EXISTS (SELECT 1 FROM backend_profile_deletions d WHERE d.profile_id = p.id)
                ORDER BY p.id LIMIT ?''',
                                (account_id, after, limit)).fetchall()
        return [self.public(row) for row in rows]

    def all_profiles(self, *, after, limit):
        with self.db.connect() as conn:
            rows = conn.execute('''SELECT p.*, CASE WHEN d.profile_id IS NULL THEN 0 ELSE 1 END AS deleting
                FROM backend_profiles p LEFT JOIN backend_profile_deletions d ON d.profile_id = p.id
                WHERE p.id > ? AND (d.profile_id IS NULL OR EXISTS (
                    SELECT 1 FROM backend_operations o WHERE o.id = d.operation_id
                    AND o.status NOT IN ('no_targets', 'succeeded')))
                ORDER BY p.id LIMIT ?''',
                                (after, limit)).fetchall()
        return [self.public(row) for row in rows]

    def search_profiles(self, term, *, after, limit):
        """Match names with Unicode case folding while paging by stable profile ID."""
        matches = []
        current = after
        with self.db.connect() as conn:
            while len(matches) < limit:
                rows = conn.execute('''SELECT p.*, CASE WHEN d.profile_id IS NULL THEN 0 ELSE 1 END AS deleting
                    FROM backend_profiles p LEFT JOIN backend_profile_deletions d ON d.profile_id = p.id
                    WHERE p.id > ? AND (d.profile_id IS NULL OR EXISTS (
                        SELECT 1 FROM backend_operations o WHERE o.id = d.operation_id
                        AND o.status NOT IN ('no_targets', 'succeeded')))
                    ORDER BY p.id LIMIT 200''', (current,)).fetchall()
                if not rows:
                    break
                for row in rows:
                    current = row['id']
                    if term in row['display_name'].casefold() or term in row['id'].casefold():
                        matches.append(self.public(row))
                        if len(matches) >= limit:
                            break
                if len(rows) < 200:
                    break
        return matches

    def grants(self, profile_id):
        with self.db.connect() as conn:
            rows = conn.execute('''SELECT node_key, protocol FROM backend_grants
                WHERE profile_id = ? ORDER BY node_key, protocol''', (profile_id,)).fetchall()
        return [{'node_key': row['node_key'], 'protocol': row['protocol']} for row in rows]

    def summary(self, profile_id):
        with self.db.connect() as conn:
            profile = conn.execute('''SELECT id, display_name, frozen, expires_at, created_at
                FROM backend_profiles WHERE id = ?''', (profile_id,)).fetchone()
            rows = conn.execute('''SELECT n.key, n.title, n.region, n.flag, g.protocol
                FROM backend_grants g JOIN backend_nodes n ON n.key = g.node_key
                WHERE g.profile_id = ? ORDER BY n.region, n.title, n.key, g.protocol''',
                (profile_id,)).fetchall()
            activity = conn.execute('''SELECT COUNT(*) AS issued_count,
                MAX(created_at) AS last_issued_at FROM backend_config_issuances
                WHERE profile_id = ? AND status = 'succeeded' ''',
                (profile_id,)).fetchone()
        nodes = {}
        for row in rows:
            entry = nodes.setdefault(row['key'], {'key': row['key'], 'title': row['title'],
                'flag': row['flag'], 'region': row['region'], 'protocols': []})
            entry['protocols'].append(row['protocol'])
        return {'profile_id': profile_id, 'display_name': profile['display_name'],
            'frozen': bool(profile['frozen']), 'expires_at': profile['expires_at'],
            'expired': bool(profile['expires_at'] and
                datetime.fromisoformat(profile['expires_at']) <= datetime.now(timezone.utc)),
            'created_at': profile['created_at'], 'nodes': list(nodes.values()),
            'node_count': len(nodes), 'protocol_count': len(rows),
            'xray_count': sum(row['protocol'] == 'xray' for row in rows),
            'awg_count': sum(row['protocol'] == 'awg' for row in rows),
            'issued_count': activity['issued_count'],
            'last_issued_at': activity['last_issued_at']}

    def available_nodes(self, account_id, *, after, limit, profile_id=None):
        now = datetime.now(timezone.utc).isoformat()
        with self.db.connect() as conn:
            rows = conn.execute('''SELECT n.key, n.title, n.region, n.flag, n.protocols_json, n.xray_transports_json
                FROM backend_nodes n WHERE n.enabled = 1 AND n.key > ?
                AND n.desired_revision = n.applied_revision
                AND NOT EXISTS (SELECT 1 FROM backend_node_drains d WHERE d.node_key = n.key)
                AND NOT EXISTS (SELECT 1 FROM backend_node_jobs j WHERE j.node_key = n.key
                    AND j.status IN ('awaiting_executor', 'running', 'blocked'))
                AND NOT EXISTS (SELECT 1 FROM backend_node_settings_tasks t WHERE t.node_key = n.key
                    AND t.status IN ('awaiting_executor', 'running', 'blocked'))
                AND EXISTS (
                    SELECT 1 FROM backend_grants g JOIN backend_profiles p ON p.id = g.profile_id
                    WHERE g.node_key = n.key AND p.owner_account_id = ? AND p.frozen = 0
                    AND (CAST(? AS TEXT) IS NULL OR p.id = ?)
                    AND n.protocols_json LIKE ('%' || '"' || g.protocol || '"' || '%')
                    AND (p.expires_at IS NULL OR p.expires_at > ?)) ORDER BY n.key LIMIT ?''',
                                (after, account_id, profile_id, profile_id, now, limit)).fetchall()
            output = []
            for node in rows:
                grants = conn.execute('''SELECT DISTINCT g.protocol FROM backend_grants g
                    JOIN backend_profiles p ON p.id = g.profile_id
                    WHERE g.node_key = ? AND p.owner_account_id = ? AND p.frozen = 0
                    AND (CAST(? AS TEXT) IS NULL OR p.id = ?)
                    AND (p.expires_at IS NULL OR p.expires_at > ?)''',
                    (node['key'], account_id, profile_id, profile_id, now)).fetchall()
                allowed = {row['protocol'] for row in grants} & set(json.loads(node['protocols_json']))
                protocols = [{'kind': kind, 'transports': sorted(set(json.loads(node['xray_transports_json'])) & {'tcp', 'xhttp'}) if kind == 'xray' else ['vpn', 'conf']}
                             for kind in sorted(allowed & {'awg', 'xray'})]
                output.append({key: node[key] for key in ('key', 'title', 'region', 'flag')} | {'protocols': protocols})
        return output


class ProfileService:
    def __init__(self, repository):
        self.repository = repository

    @staticmethod
    def page_input(limit, cursor, kind):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AccessDenied('invalid_input', 422)
        return _cursor(cursor, kind)

    def list_owned(self, actor, *, limit=25, cursor=None):
        require_permission(actor, 'profiles.self.read')
        after = self.page_input(limit, cursor, 'profiles')
        return _page(self.repository.owned(actor.account.id, after=after, limit=limit + 1), limit, 'profiles', 'id')

    def get(self, actor, profile_id):
        row = self.repository.get(profile_id)
        if row is None:
            raise AccessDenied('resource_not_found', 404)
        resource = ProfileResource(row['id'], row['owner_account_id'], bool(row['frozen']))
        if row['owner_account_id'] != actor.account.id and actor.account.role != 'admin':
            raise AccessDenied('resource_not_found', 404)
        if row['deleting'] and actor.account.role != 'admin':
            raise AccessDenied('resource_not_found', 404)
        require_profile(actor, resource, administrative=row['owner_account_id'] != actor.account.id)
        return self.repository.public(row)

    def list_all(self, actor, *, limit=25, cursor=None, search=None):
        require_permission(actor, 'profiles.manage')
        if search is not None and (not isinstance(search, str) or not 1 <= len(search.strip()) <= 128):
            raise AccessDenied('invalid_input', 422)
        term = search.strip().casefold() if search else None
        kind = 'admin_profiles_search:' + term if term else 'admin_profiles'
        after = self.page_input(limit, cursor, kind)
        rows = (self.repository.search_profiles(term, after=after, limit=limit + 1)
                if term else self.repository.all_profiles(after=after, limit=limit + 1))
        return _page(rows, limit, kind, 'id')

    def grants(self, actor, profile_id):
        self.get(actor, profile_id)
        return {'items': self.repository.grants(profile_id)}

    def own_summary(self, actor, profile_id):
        require_permission(actor, 'profiles.self.read')
        profile = self.get(actor, profile_id)
        if profile['owner_account_id'] != actor.account.id:
            raise AccessDenied('resource_not_found', 404)
        from .traffic import TrafficService
        return {**self.repository.summary(profile_id),
                'traffic': TrafficService(self.repository.db).summary(actor.account.id, profile_id)}

    def available_nodes(self, actor, *, limit=25, cursor=None):
        require_permission(actor, 'nodes.available.read')
        after = self.page_input(limit, cursor, 'nodes')
        return _page(self.repository.available_nodes(actor.account.id, after=after, limit=limit + 1), limit, 'nodes', 'key')

    def profile_nodes(self, actor, profile_id, *, limit=25, cursor=None):
        self.get(actor, profile_id)
        require_permission(actor, 'nodes.available.read')
        after = self.page_input(limit, cursor, 'nodes')
        return _page(self.repository.available_nodes(actor.account.id, after=after,
            limit=limit + 1, profile_id=profile_id), limit, 'nodes', 'key')
