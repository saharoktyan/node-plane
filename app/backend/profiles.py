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

    def initialize_schema(self):
        with self.db.transaction() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_profiles (
                id TEXT PRIMARY KEY,
                runtime_name TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                owner_account_id TEXT REFERENCES backend_accounts(id),
                frozen INTEGER NOT NULL DEFAULT 0 CHECK(frozen IN (0, 1)),
                expires_at TEXT,
                desired_revision INTEGER NOT NULL DEFAULT 1
            )''')
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
            conn.execute('CREATE INDEX IF NOT EXISTS idx_backend_profile_owner ON backend_profiles(owner_account_id, id)')
        from .node_lifecycle import NodeLifecycle
        NodeLifecycle(self.db).initialize_schema()

    def create_profile(self, *, runtime_name, display_name, owner_account_id=None):
        # Trusted local provisioning only, not an unauthenticated HTTP route.
        if not isinstance(runtime_name, str) or not runtime_name or len(runtime_name) > 64 or not all(c.isascii() and (c.isalnum() or c in '_-') for c in runtime_name):
            raise ValueError('invalid runtime_name')
        if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 128:
            raise ValueError('invalid display_name')
        profile_id = str(uuid4())
        with self.db.transaction() as conn:
            if owner_account_id is not None and conn.execute('SELECT id FROM backend_accounts WHERE id = ?', (owner_account_id,)).fetchone() is None:
                raise ValueError('owner account does not exist')
            conn.execute('INSERT INTO backend_profiles(id, runtime_name, display_name, owner_account_id) VALUES (?, ?, ?, ?)',
                         (profile_id, runtime_name, display_name, owner_account_id))
        return profile_id

    @staticmethod
    def public(row):
        return {key: row[key] for key in ('id', 'display_name', 'owner_account_id', 'expires_at', 'desired_revision')} | {'frozen': bool(row['frozen'])}

    def get(self, profile_id):
        with self.db.connect() as conn:
            return conn.execute('SELECT * FROM backend_profiles WHERE id = ?', (profile_id,)).fetchone()

    def owned(self, account_id, *, after, limit):
        with self.db.connect() as conn:
            rows = conn.execute('SELECT * FROM backend_profiles WHERE owner_account_id = ? AND id > ? ORDER BY id LIMIT ?',
                                (account_id, after, limit)).fetchall()
        return [self.public(row) for row in rows]

    def all_profiles(self, *, after, limit):
        with self.db.connect() as conn:
            rows = conn.execute('SELECT * FROM backend_profiles WHERE id > ? ORDER BY id LIMIT ?',
                                (after, limit)).fetchall()
        return [self.public(row) for row in rows]

    def grants(self, profile_id):
        with self.db.connect() as conn:
            rows = conn.execute('''SELECT node_key, protocol FROM backend_grants
                WHERE profile_id = ? ORDER BY node_key, protocol''', (profile_id,)).fetchall()
        return [{'node_key': row['node_key'], 'protocol': row['protocol']} for row in rows]

    def available_nodes(self, account_id, *, after, limit, profile_id=None):
        now = datetime.now(timezone.utc).isoformat()
        with self.db.connect() as conn:
            rows = conn.execute('''SELECT n.key, n.title, n.region, n.flag, n.protocols_json, n.xray_transports_json
                FROM backend_nodes n WHERE n.enabled = 1 AND n.key > ? AND EXISTS (
                    SELECT 1 FROM backend_grants g JOIN backend_profiles p ON p.id = g.profile_id
                    WHERE g.node_key = n.key AND p.owner_account_id = ? AND p.frozen = 0
                    AND (? IS NULL OR p.id = ?)
                    AND n.protocols_json LIKE ('%' || '"' || g.protocol || '"' || '%')
                    AND (p.expires_at IS NULL OR p.expires_at > ?)) ORDER BY n.key LIMIT ?''',
                                (after, account_id, profile_id, profile_id, now, limit)).fetchall()
            output = []
            for node in rows:
                grants = conn.execute('''SELECT DISTINCT g.protocol FROM backend_grants g
                    JOIN backend_profiles p ON p.id = g.profile_id
                    WHERE g.node_key = ? AND p.owner_account_id = ? AND p.frozen = 0
                    AND (? IS NULL OR p.id = ?)
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
        require_profile(actor, resource, administrative=row['owner_account_id'] != actor.account.id)
        return self.repository.public(row)

    def list_all(self, actor, *, limit=25, cursor=None):
        require_permission(actor, 'profiles.manage')
        after = self.page_input(limit, cursor, 'admin_profiles')
        return _page(self.repository.all_profiles(after=after, limit=limit + 1),
                     limit, 'admin_profiles', 'id')

    def grants(self, actor, profile_id):
        self.get(actor, profile_id)
        return {'items': self.repository.grants(profile_id)}

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
