"""Database-backed administration summary; runtime health needs an explicit probe."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from config import UPDATE_BRANCH

from .authorization import require_permission


class AdminOverviewService:
    def __init__(self, db):
        self.db = db

    def get(self, actor):
        require_permission(actor, 'profiles.manage')
        with self.db.connect() as conn:
            from .alerts import AlertService
            alert_count = sum(not r['dismissed'] for r in AlertService.active(conn, actor.account.id))
            nodes = conn.execute('SELECT key, title, region, flag, enabled FROM backend_nodes ORDER BY region, title, key').fetchall()
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
                UNION SELECT node_key FROM backend_node_jobs WHERE status='blocked'
                UNION SELECT node_key FROM backend_agent_rollouts WHERE status='blocked'
                UNION SELECT node_key FROM backend_node_bootstraps WHERE status='blocked'
                UNION SELECT node_key FROM backend_node_removals WHERE status='blocked'
            ''').fetchall()}
            from .recovery import _sql
            task_counts = conn.execute(f'''SELECT status,COUNT(*) AS count
                FROM ({_sql()}) tasks GROUP BY status''').fetchall()
            policy = conn.execute("SELECT value FROM backend_system_settings WHERE key='backup_policy'").fetchone()
            backup = conn.execute("SELECT status,created_at FROM backend_backup_jobs WHERE action='create' ORDER BY created_at DESC,id DESC LIMIT 1").fetchone()
            meta = {row['key']: row['value'] for row in conn.execute(
                "SELECT key,value FROM schema_meta WHERE key IN ('updates_update_available','updates_remote_version','updates_last_checked_at','updates_last_status','updates_branch')").fetchall()}
        now = datetime.now(timezone.utc)
        active = sum(not p['deleting'] and not p['frozen'] and
            (p['expires_at'] is None or datetime.fromisoformat(p['expires_at']) > now)
            for p in profiles)
        return {
            'unacknowledged_alerts': alert_count,
            'checked_at': now.isoformat(),
            'tasks': {key: sum(row['count'] for row in task_counts if row['status'] in states)
                      for key, states in {'queued': {'queued','awaiting_executor'},
                          'running': {'running','awaiting_shutdown'}, 'blocked': {'blocked'}}.items()},
            'backup': {'enabled': bool(json.loads(policy['value']).get('enabled')) if policy else False,
                       'last_job': dict(backup) if backup else None},
            'updates': {'available': meta.get('updates_update_available') == '1',
                        'latest_version': meta.get('updates_remote_version') or None,
                        'last_checked_at': meta.get('updates_last_checked_at') or None,
                        'status': meta.get('updates_last_status', 'never'),
                        'branch': meta.get('updates_branch', UPDATE_BRANCH)},
            'nodes_total': len(nodes),
            'nodes_enabled': sum(bool(node['enabled']) for node in nodes),
            'profiles_total': sum(not p['deleting'] for p in profiles),
            'profiles_active': active,
            'profiles_frozen': sum(not p['deleting'] and bool(p['frozen']) for p in profiles),
            'pending_requests': pending_requests,
            'problem_nodes': [{'key': node['key'], 'title': node['title'], 'region': node['region'], 'flag': node['flag']}
                              for node in nodes if node['key'] in problem_keys],
        }
