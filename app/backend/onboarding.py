"""First-node guide derived from successful controller operations."""
import json
from datetime import datetime, timezone

from .authorization import require_permission, AccessDenied


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
        ('config', issued))]
    for client in ('telegram', 'workstation'):
        required = [step for step in steps if client == 'telegram' or step['key'] != 'config']
        if all(step['complete'] for step in required):
            with db.transaction() as conn:
                conn.execute('INSERT INTO backend_system_settings(key,value) VALUES (?,?) ON CONFLICT(key) DO NOTHING',
                    ('onboarding.completed.' + client + '.' + actor.account.id, '1'))
    return {'completed': completed(db, actor.account.id, 'telegram'),
            'workstation_completed': completed(db, actor.account.id, 'workstation'), 'steps': steps, 'next_step': next((s['key'] for s in steps if not s['complete']), None),
            'node_key': node['key'] if node else None, 'profile_id': profile['id'] if profile else None}


def completed(db, account_id, client):
    with db.connect() as conn:
        return bool(conn.execute("SELECT 1 FROM backend_system_settings WHERE key=?",
            ('onboarding.completed.' + client + '.' + account_id,)).fetchone())


def finish(db, actor, client):
    require_permission(actor, 'nodes.manage')
    value = progress(db, actor)
    if client not in {'telegram', 'workstation'}:
        raise AccessDenied('invalid_input', 422)
    if not completed(db, actor.account.id, client):
        required = [step for step in value['steps'] if client == 'telegram' or step['key'] != 'config']
        if not all(step['complete'] for step in required):
            raise AccessDenied('onboarding_incomplete', 409)
        with db.transaction() as conn:
            conn.execute('INSERT INTO backend_system_settings(key,value) VALUES (?,?) ON CONFLICT(key) DO NOTHING',
                ('onboarding.completed.' + client + '.' + actor.account.id, '1'))
    return progress(db, actor)


def dismiss(db, actor, client):
    require_permission(actor, 'nodes.manage')
    if client not in {'telegram', 'workstation'}:
        raise AccessDenied('invalid_input', 422)
    with db.transaction() as conn:
        conn.execute('INSERT INTO backend_system_settings(key,value) VALUES (?,?) ON CONFLICT(key) DO NOTHING',
            ('onboarding.completed.' + client + '.' + actor.account.id, '1'))
    return progress(db, actor)
