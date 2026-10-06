"""Fast persisted node summary; a live probe is a separate explicit action."""
from __future__ import annotations

from datetime import datetime, timezone

from .authorization import AccessDenied, require_permission
from .node_settings import _snapshot


class NodeOverviewService:
    def __init__(self, db):
        self.db = db

    def get(self, actor, node_key):
        require_permission(actor, 'nodes.manage')
        with self.db.connect() as conn:
            node = conn.execute('SELECT * FROM backend_nodes WHERE key = ?', (node_key,)).fetchone()
            if node is None:
                raise AccessDenied('resource_not_found', 404)
            settings_tasks = conn.execute('''SELECT revision, status FROM backend_node_settings_tasks
                WHERE node_key = ? ORDER BY revision DESC, id DESC''', (node_key,)).fetchall()
            grants = conn.execute('''SELECT g.profile_id, g.protocol, p.frozen, p.expires_at,
                p.desired_revision, d.profile_id AS deleting
                FROM backend_grants g JOIN backend_profiles p ON p.id = g.profile_id
                LEFT JOIN backend_profile_deletions d ON d.profile_id = p.id
                WHERE g.node_key = ?''', (node_key,)).fetchall()
            tasks = conn.execute('''SELECT o.profile_id, o.desired_revision, t.protocol,
                t.action, t.status FROM backend_operation_tasks t
                JOIN backend_operations o ON o.id = t.operation_id
                WHERE t.node_key = ?
                ORDER BY o.desired_revision DESC, o.created_at DESC, t.id DESC''',
                (node_key,)).fetchall()
            job = conn.execute('SELECT id, action, revision, status FROM backend_node_jobs WHERE node_key = ? ORDER BY revision DESC, id DESC LIMIT 1', (node_key,)).fetchone()
            removal = conn.execute('SELECT status FROM backend_node_removals WHERE node_key = ?', (node_key,)).fetchone()

        current_settings = next((task['status'] for task in settings_tasks
            if task['revision'] == node['desired_revision']), None)
        try:
            _snapshot(node)
            settings_complete = True
        except AccessDenied:
            settings_complete = False
        if node['applied_revision'] == 0 and current_settings not in {'running', 'blocked'}:
            state = 'not_installed'
        elif current_settings in {'awaiting_executor', 'running'}:
            state = 'applying'
        elif current_settings == 'blocked':
            state = 'needs_attention'
        elif node['applied_revision'] < node['desired_revision']:
            state = 'changes_pending'
        elif not node['enabled']:
            state = 'inactive'
        else:
            state = 'applied_unverified'
        if job and job['status'] in {'awaiting_executor', 'running', 'blocked'}:
            state = 'needs_attention' if job['status'] == 'blocked' else 'applying'
        removal_status = removal['status'] if removal else None
        if removal_status in {'queued', 'running', 'blocked'}:
            state = 'deletion_blocked' if removal_status == 'blocked' else 'deleting'

        latest = {}
        for task in tasks:
            latest.setdefault((task['profile_id'], task['protocol']), task)
        now = datetime.now(timezone.utc)
        counts = {'ready': 0, 'pending': 0, 'failed': 0, 'attention': 0}
        for grant in grants:
            task = latest.get((grant['profile_id'], grant['protocol']))
            expires_at = grant['expires_at']
            active = not grant['frozen'] and not grant['deleting'] and (
                expires_at is None or datetime.fromisoformat(expires_at) > now)
            if not active:
                counts['attention'] += 1
            elif task is None or task['desired_revision'] != grant['desired_revision']:
                counts['attention'] += 1
            elif task['action'] != 'ensure':
                counts['attention'] += 1
            elif task['status'] == 'blocked':
                counts['failed'] += 1
            elif task['status'] in {'awaiting_executor', 'running'}:
                counts['pending'] += 1
            elif task['status'] == 'succeeded' and state == 'applied_unverified':
                counts['ready'] += 1
            else:
                counts['attention'] += 1

        return {'node_key': node['key'], 'enabled': bool(node['enabled']),
                'state': state, 'desired_revision': node['desired_revision'],
                'applied_revision': node['applied_revision'],
                'settings_task_status': current_settings,
                'removal_status': removal_status,
                'settings_complete': settings_complete,
                'access_total': len(grants), 'last_job': dict(job) if job else None, **counts}
