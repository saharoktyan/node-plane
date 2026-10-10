"""Secret-free diagnostics and operation-specific recovery; never delete fences."""
import json
from contextlib import contextmanager
import re
from datetime import datetime, timezone
from uuid import UUID, uuid4

from .authorization import AccessDenied, require_permission

SOURCES = (
    ('update', 'backend_update_jobs', "''", 'result_json'),
    ('node', 'backend_node_jobs', 'node_key', 'result_json'),
    ('settings', 'backend_node_settings_tasks', 'node_key', 'result_json'),
    ('bootstrap', 'backend_node_bootstraps', 'node_key', 'NULL'),
    ('agent', 'backend_agent_rollouts', 'node_key', 'NULL'),
    ('profile', 'backend_operation_tasks', 'node_key', 'result_json'),
    ('removal', 'backend_node_removals', 'node_key', 'NULL'),
    ('backup', 'backend_backup_jobs', "''", 'result_json'),
)
TABLES = {kind: table for kind, table, *_ in SOURCES}


def initialize_schema(db):
    from db.migration_revisions.r0005_recovery_audit import upgrade
    with db.transaction() as conn:
        upgrade(conn)


def _json(value):
    try:
        result = json.loads(value or '{}')
    except (ValueError, TypeError):
        return {}
    return result if isinstance(result, dict) else {}


def _maintenance(conn):
    gate = conn.execute('SELECT job_id FROM backend_controller_update_gate WHERE id=1').fetchone()
    other = bool(conn.execute("SELECT id FROM backend_system_cleanup_jobs WHERE status IN ('queued','running','awaiting_shutdown','blocked') LIMIT 1").fetchone()) or bool(conn.execute("SELECT id FROM backend_backup_jobs WHERE action='restore' AND status IN ('awaiting_executor','running') LIMIT 1").fetchone())
    return gate, other


def _item(conn, row, actor, gate, other):
    kind, identity, status = row['kind'], row['id'], row['status']
    result = _json(row['result_json'])
    code = result.get('error_code') or row['stored_error'] or ''
    phase, subject, related_id, related_kind = '', '', '', ''
    if kind == 'agent':
        failure = conn.execute('SELECT code FROM backend_agent_rollout_failures WHERE task_id=?', (identity,)).fetchone()
        code = failure['code'] if failure else code
    elif kind == 'bootstrap':
        job = conn.execute('SELECT phase,child_id,child_kind FROM backend_node_bootstraps WHERE id=?', (identity,)).fetchone()
        phase, related_id, related_kind = job['phase'], job['child_id'] or '', job['child_kind'] or ''
    elif kind == 'profile':
        profile = conn.execute('SELECT o.profile_id FROM backend_operations o JOIN backend_operation_tasks t ON t.operation_id=o.id WHERE t.id=?', (identity,)).fetchone()
        subject = profile['profile_id'] if profile else ''
    code = code if isinstance(code, str) and re.fullmatch(r'[a-z0-9_]{1,100}', code) else ''
    code = code or ('outcome_unconfirmed' if status == 'blocked' else 'waiting_worker' if status in {'queued', 'awaiting_executor'} else 'operation_running')
    actions = []
    if kind == 'update' and not other and (gate is None or gate['job_id'] == identity):
        if status == 'awaiting_executor' and not conn.execute("SELECT 1 FROM backend_update_items WHERE job_id=? AND (status!='awaiting_executor' OR child_id IS NOT NULL) LIMIT 1", (identity,)).fetchone():
            actions.append('cancel')
        job = conn.execute('SELECT kind FROM backend_update_jobs WHERE id=?', (identity,)).fetchone()
        if status == 'blocked' and job['kind'] == 'stack' and result.get('phase') == 'core' and result.get('unit_name'):
            actions.append('recheck')
    elif status == 'blocked' and not gate and not other:
        if kind in {'node', 'settings'} and 'nodes.manage' in actor.principal.scopes:
            actions = ['recheck', 'resolve']
            if kind == 'settings' and any(conn.execute(f"SELECT 1 FROM {table} WHERE node_key=? AND status IN ('awaiting_executor','running','blocked') LIMIT 1", (row['node_key'],)).fetchone() for table in ('backend_node_jobs','backend_agent_rollouts','backend_node_bootstraps','backend_operation_tasks')):
                actions.remove('resolve')
        elif kind == 'profile' and 'profiles.manage' in actor.principal.scopes:
            actions = ['recheck', 'resolve']
        elif kind in {'agent', 'bootstrap'} and 'nodes.manage' in actor.principal.scopes:
            actions = ['recheck']
            if kind == 'bootstrap' and not related_id:
                actions.append('resolve')
    hint = ('maintenance' if (gate or other) and kind != 'update' else
            'child' if kind == 'bootstrap' and related_id else
            'agent' if kind == 'agent' else
            'bootstrap' if kind == 'bootstrap' else
            'journal' if actions else kind if status == 'blocked' else 'worker')
    return {'id': identity, 'kind': kind, 'status': status, 'node_key': row['node_key'] or '',
            'error_code': code, 'actions': actions, 'phase': phase, 'subject_id': subject,
            'related_id': related_id, 'related_kind': related_kind, 'next_step': hint}


def _sql():
    return ' UNION ALL '.join(
        f"SELECT '{kind}' AS kind,{'node_key' if kind == 'removal' else 'id'} AS id,status,{node} AS node_key,{result} AS result_json,"
        f"{('error_code' if kind in {'removal', 'bootstrap'} else 'NULL')} AS stored_error FROM {table} "
        "WHERE status IN ('queued','awaiting_executor','running','blocked','awaiting_shutdown')"
        for kind, table, node, result in SOURCES)


def overview(db, actor, offset=0):
    require_permission(actor, 'maintenance.manage')
    page_size, sql = 10, _sql()
    with db.connect() as conn:
        gate, other = _maintenance(conn)
        total = conn.execute(f'SELECT COUNT(*) AS count FROM ({sql}) inventory').fetchone()['count']
        offset = min(offset, max(0, (total - 1) // page_size * page_size))
        rows = conn.execute(f'{sql} ORDER BY kind,id LIMIT ? OFFSET ?', (page_size, offset)).fetchall()
        items = [_item(conn, row, actor, gate, other) for row in rows]
    return {'items': items, 'offset': offset, 'page_size': page_size, 'total': total,
            'maintenance_active': bool(gate) or other}


def act(db, actor, driver, kind, identity, action):
    """Caller holds worker flock. Only the named command can change."""
    require_permission(actor, 'maintenance.manage')
    if kind not in {'node', 'settings', 'profile', 'agent', 'bootstrap'} or action not in {'recheck', 'resolve'}:
        raise AccessDenied('invalid_input', 422)
    try:
        identity = str(UUID(identity))
    except (ValueError, TypeError):
        raise AccessDenied('invalid_input', 422) from None
    table = TABLES[kind]
    with db.transaction() as conn:
        from .maintenance_gate import admit
        admit(conn)
        account = conn.execute('UPDATE backend_accounts SET role=role WHERE id=? RETURNING role,status', (actor.account.id,)).fetchone()
        if not account or account['role'] != 'admin' or account['status'] != 'approved':
            raise AccessDenied('permission_denied')
        row = conn.execute(f'SELECT * FROM {table} WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise AccessDenied('resource_not_found', 404)
        if row['status'] != 'blocked':
            raise AccessDenied('task_not_blocked', 409)
        gate, other = _maintenance(conn)
        inventory = conn.execute(f"SELECT * FROM ({_sql()}) inventory WHERE kind=? AND id=?", (kind, identity)).fetchone()
        if action not in _item(conn, inventory, actor, gate, other)['actions']:
            raise AccessDenied('permission_denied')
        event = str(uuid4())
        conn.execute('INSERT INTO backend_recovery_audit VALUES (?,?,?,?,?,?,?,?)',
            (event, actor.account.id, kind, identity, action, 'admitted', row['status'], datetime.now(timezone.utc).isoformat()))
    observation, replacement = None, None
    try:
        if kind == 'node':
            from .node_operations import NodeOperations
            service = NodeOperations(db, driver)
            if action == 'resolve':
                service.resolve(actor, identity)
            else:
                service._finish(row, driver.node_action(identity, row['action'], _json(row['intent_json']), recover=True))
        elif kind == 'settings':
            from .node_settings import NodeSettingsExecutor
            service = NodeSettingsExecutor(db, driver)
            if action == 'resolve':
                replacement = service.resolve_blocked(actor, identity)['task_id']
            else:
                result = driver.recover_node_settings(identity, _json(row['intent_json']))
                with db.transaction() as conn:
                    current = conn.execute(f"SELECT * FROM {table} WHERE id=? AND status='blocked'", (identity,)).fetchone()
                    if current is not None:
                        service._finish(conn, current, result)
        elif kind == 'profile':
            from .executor import IntentExecutor
            service = IntentExecutor(db, driver)
            if action == 'resolve':
                replacement = service.resolve_blocked(actor, identity)['operation_id']
            else:
                result = driver.lookup(identity, _json(row['intent_json']))
                if result.get('succeeded') is True:
                    with db.transaction() as conn:
                        changed = conn.execute("UPDATE backend_operation_tasks SET status='succeeded',driver_operation_id=?,result_json=? WHERE id=? AND status='blocked' RETURNING id", (result['driver_operation_id'], result.get('result_json'), identity)).fetchone()
                        if changed:
                            service.refresh_operation(conn, row['operation_id'])
        elif kind == 'agent':
            # Confirm the bound, authenticated agent and installed build independently.
            facts = driver.inspect_node(row['node_key'])
            if facts.get('node_key') != row['node_key']:
                raise ValueError('agent identity mismatch')
            from config import APP_COMMIT
            from .updates import same_commit
            services = driver.inspect_node_services(row['node_key'])
            current = bool(APP_COMMIT and same_commit(services.get('agent_commit'), APP_COMMIT))
            observation = {'agent_reachable': True, 'agent_current': current}
            with db.transaction() as conn:
                connection = conn.execute('SELECT transport,ssh_target FROM backend_node_connections WHERE node_key=?', (row['node_key'],)).fetchone()
                intent = _json(row['intent_json'])
                bound = connection is not None and (connection['transport'], connection['ssh_target']) == (intent.get('transport'), intent.get('ssh_target'))
                if current and bound:
                    conn.execute("UPDATE backend_agent_rollouts SET status='succeeded' WHERE id=? AND status='blocked'", (identity,))
        elif kind == 'bootstrap':
            if action == 'resolve':
                from .node_bootstrap import NodeBootstrapService
                NodeBootstrapService(db, driver).resume(actor, identity)
            elif row['child_id']:
                child_table = {'agent-rollouts': 'backend_agent_rollouts', 'node-jobs': 'backend_node_jobs'}.get(row['child_kind'])
                with db.connect() as conn:
                    child = conn.execute(f'SELECT status FROM {child_table} WHERE id=?', (row['child_id'],)).fetchone() if child_table else None
                if child is not None and child['status'] in {'succeeded', 'superseded'}:
                    from .node_bootstrap import NodeBootstrapService
                    # Existing terminal child only: this branch never dispatches a new child.
                    NodeBootstrapService(db, driver)._advance(row)
            observation = None
    except AccessDenied:
        _audit_result(db, event, 'refused', table, identity)
        raise
    except Exception:
        _audit_result(db, event, 'unconfirmed', table, identity)
        raise AccessDenied('recovery_unconfirmed', 409) from None
    status = _audit_result(db, event, 'confirmed', table, identity)
    return {'id': identity, 'kind': kind, 'status': status,
            'outcome': 'still_blocked' if status == 'blocked' else 'confirmed',
            'replacement_id': replacement, 'observation': observation}


def _audit_result(db, event, outcome, table, identity):
    with db.transaction() as conn:
        status = conn.execute(f'SELECT status FROM {table} WHERE id=?', (identity,)).fetchone()['status']
        conn.execute('UPDATE backend_recovery_audit SET outcome=?,status=? WHERE id=?',
                     ('still_blocked' if outcome == 'confirmed' and status == 'blocked' else outcome, status, event))
    return status


def history(db, actor, offset=0):
    require_permission(actor, 'maintenance.manage')
    with db.connect() as conn:
        total = conn.execute('SELECT COUNT(*) AS count FROM backend_recovery_audit').fetchone()['count']
        offset = min(offset, max(0, (total - 1) // 10 * 10))
        rows = conn.execute('SELECT * FROM backend_recovery_audit ORDER BY created_at DESC,id DESC LIMIT 10 OFFSET ?', (offset,)).fetchall()
    return {'items': [dict(row) for row in rows], 'offset': offset, 'page_size': 10, 'total': total}


def failure_code(error):
    """Only fixed transport categories, never raw SSH/gRPC exception text."""
    import grpc
    if isinstance(error, grpc.RpcError):
        if error.code() == grpc.StatusCode.UNAVAILABLE:
            return 'node_agent_unavailable'
        if error.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
            return 'operation_timeout'
        if error.code() == grpc.StatusCode.FAILED_PRECONDITION:
            if error.details() in {'no node-agent target configured', 'agent not configured'}:
                return 'node_agent_unconfigured'
            return 'journal_unconfirmed'
    if isinstance(error, TimeoutError):
        return 'operation_timeout'
    return 'outcome_unconfirmed'


@contextmanager
def track(db, actor, kind, identity, action):
    """Audit existing repair routes, including attempts with unknown outcomes."""
    require_permission(actor, 'maintenance.manage')
    identity = str(UUID(identity))
    event = str(uuid4())
    table = TABLES[kind]
    with db.transaction() as conn:
        row = conn.execute(f'SELECT status FROM {table} WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise AccessDenied('resource_not_found', 404)
        conn.execute('INSERT INTO backend_recovery_audit VALUES (?,?,?,?,?,?,?,?)',
            (event,actor.account.id,kind,identity,action,'admitted',row['status'],datetime.now(timezone.utc).isoformat()))
    try:
        yield
    except Exception:
        _audit_result(db,event,'unconfirmed',table,identity)
        raise
    else:
        _audit_result(db,event,'confirmed',table,identity)
