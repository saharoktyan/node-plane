"""Read-only maintenance inventory. Recovery uses existing operation contracts."""
import json
import re

from .authorization import require_permission


SOURCES = (
    ('update', 'backend_update_jobs', "''", 'result_json'),
    ('node', 'backend_node_jobs', 'node_key', 'result_json'),
    ('agent', 'backend_agent_rollouts', 'node_key', 'NULL'),
    ('profile', 'backend_operation_tasks', 'node_key', 'result_json'),
    ('removal', 'backend_node_removals', 'node_key', 'NULL'),
    ('backup', 'backend_backup_jobs', "''", 'result_json'),
)


def overview(db, actor, offset=0):
    require_permission(actor, 'maintenance.manage')
    page_size = 10
    sql = ' UNION ALL '.join(
        f"SELECT '{kind}' AS kind,{'node_key' if kind == 'removal' else 'id'} AS id,status,{node} AS node_key,{result} AS result_json,"
        f"{('error_code' if kind == 'removal' else 'NULL')} AS stored_error FROM {table} "
        "WHERE status IN ('queued','awaiting_executor','running','blocked','awaiting_shutdown')"
        for kind, table, node, result in SOURCES)
    with db.connect() as conn:
        gate = conn.execute('SELECT job_id FROM backend_controller_update_gate WHERE id=1').fetchone()
        other_maintenance = bool(conn.execute("SELECT id FROM backend_system_cleanup_jobs WHERE status IN ('queued','running','awaiting_shutdown','blocked') LIMIT 1").fetchone()) or bool(conn.execute("SELECT id FROM backend_backup_jobs WHERE action='restore' AND status IN ('awaiting_executor','running') LIMIT 1").fetchone())
        maintenance = bool(gate) or other_maintenance
        total = conn.execute(f'SELECT COUNT(*) AS count FROM ({sql}) inventory').fetchone()['count']
        # Keep a stale last-page callback usable after recovery shrinks the list.
        offset = min(offset, max(0, (total - 1) // page_size * page_size))
        rows = conn.execute(f'{sql} ORDER BY kind,id LIMIT ? OFFSET ?', (page_size, offset)).fetchall()
        items = []
        for row in rows:
            try:
                result = json.loads(row['result_json'] or '{}')
            except (ValueError, TypeError):
                result = {}
            if not isinstance(result, dict):
                result = {}
            actions = []
            if row['kind'] == 'update' and not other_maintenance and (gate is None or gate['job_id'] == row['id']):
                if row['status'] == 'awaiting_executor' and not conn.execute("SELECT 1 FROM backend_update_items WHERE job_id=? AND (status!='awaiting_executor' OR child_id IS NOT NULL) LIMIT 1", (row['id'],)).fetchone():
                    actions.append('cancel')
                job = conn.execute('SELECT kind FROM backend_update_jobs WHERE id=?', (row['id'],)).fetchone()
                if row['status'] == 'blocked' and job['kind'] == 'stack' and result.get('phase') == 'core' and result.get('unit_name'):
                    actions.append('recheck')
            elif row['kind'] == 'node' and row['status'] == 'blocked' and not maintenance and 'nodes.manage' in actor.principal.scopes:
                actions.append('resolve')
            code = result.get('error_code') or row['stored_error'] or ''
            code = code if isinstance(code, str) and re.fullmatch(r'[a-z0-9_]{1,100}', code) else ''
            items.append({'id': row['id'], 'kind': row['kind'], 'status': row['status'],
                          'node_key': row['node_key'] or '', 'error_code': code, 'actions': actions})
    return {'items': items, 'offset': offset, 'page_size': page_size, 'total': total,
            'maintenance_active': maintenance}
