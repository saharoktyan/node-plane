"""First-node guide derived from successful controller operations."""
import json
from datetime import datetime, timezone

from .authorization import require_permission


def progress(db, actor):
    require_permission(actor, 'nodes.manage')
    with db.connect() as conn:
        configured = bool(conn.execute("SELECT 1 FROM backend_system_settings WHERE key='installation_defaults'").fetchone())
        node = conn.execute('''SELECT n.* FROM backend_nodes n
            LEFT JOIN backend_node_connections c ON c.node_key=n.key
            ORDER BY CASE WHEN COALESCE(c.transport,'local')='local' THEN 0 ELSE 1 END,
                CASE WHEN n.applied_revision=n.desired_revision AND n.enabled=1 THEN 0 ELSE 1 END,n.key LIMIT 1''').fetchone()
        applied = bool(node and node['enabled'] and node['applied_revision'] == node['desired_revision']
                       and json.loads(node['protocols_json']))
        profile = None
        if node:
            profile = conn.execute('''SELECT p.* FROM backend_profiles p
                WHERE p.owner_account_id=? AND p.frozen=0
                AND (p.expires_at IS NULL OR p.expires_at>?)
                AND NOT EXISTS (SELECT 1 FROM backend_profile_deletions d WHERE d.profile_id=p.id)
                AND EXISTS (SELECT 1 FROM backend_grants g WHERE g.profile_id=p.id AND g.node_key=?)
                ORDER BY p.id LIMIT 1''', (actor.account.id, datetime.now(timezone.utc).isoformat(), node['key'])).fetchone()
        issued = bool(profile and conn.execute('''SELECT 1 FROM backend_config_issuances
            WHERE profile_id=? AND node_key=? AND status='succeeded'
            AND profile_revision=? AND node_revision=? LIMIT 1''',
            (profile['id'], node['key'], profile['desired_revision'], node['desired_revision'])).fetchone())
    steps = [{'key': key, 'complete': bool(complete)} for key, complete in (
        ('defaults', configured), ('node', node is not None), ('protocols', applied),
        ('access', profile is not None), ('config', issued))]
    return {'steps': steps, 'next_step': next((s['key'] for s in steps if not s['complete']), None),
            'node_key': node['key'] if node else None, 'profile_id': profile['id'] if profile else None}
