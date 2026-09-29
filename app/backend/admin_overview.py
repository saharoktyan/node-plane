"""Database-backed administration summary; runtime health needs an explicit probe."""
from __future__ import annotations

from datetime import datetime, timezone

from .authorization import require_permission


class AdminOverviewService:
    def __init__(self, db):
        self.db = db

    def get(self, actor):
        require_permission(actor, 'profiles.manage')
        with self.db.connect() as conn:
            nodes = conn.execute('SELECT key, title, enabled FROM backend_nodes ORDER BY key').fetchall()
            profiles = conn.execute('''SELECT p.frozen, p.expires_at, d.profile_id AS deleting
                FROM backend_profiles p LEFT JOIN backend_profile_deletions d ON d.profile_id = p.id''').fetchall()
            pending_requests = conn.execute("SELECT COUNT(*) AS n FROM backend_access_requests WHERE status = 'pending'").fetchone()['n']
            problem_keys = {row['node_key'] for row in conn.execute('''
                SELECT t.node_key FROM backend_operation_tasks t
                JOIN backend_operations o ON o.id = t.operation_id
                JOIN backend_profiles p ON p.id = o.profile_id
                WHERE t.status = 'blocked' AND o.desired_revision = p.desired_revision
                UNION SELECT s.node_key FROM backend_node_settings_tasks s
                JOIN backend_nodes n ON n.key = s.node_key
                WHERE s.status = 'blocked' AND s.revision = n.desired_revision
            ''').fetchall()}
        now = datetime.now(timezone.utc)
        active = sum(not p['deleting'] and not p['frozen'] and
            (p['expires_at'] is None or datetime.fromisoformat(p['expires_at']) > now)
            for p in profiles)
        return {
            'nodes_total': len(nodes),
            'nodes_enabled': sum(bool(node['enabled']) for node in nodes),
            'profiles_total': sum(not p['deleting'] for p in profiles),
            'profiles_active': active,
            'profiles_frozen': sum(not p['deleting'] and bool(p['frozen']) for p in profiles),
            'pending_requests': pending_requests,
            'problem_nodes': [{'key': node['key'], 'title': node['title']}
                              for node in nodes if node['key'] in problem_keys],
        }
