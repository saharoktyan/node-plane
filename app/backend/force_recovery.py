"""Explicit cancellation, preserving remote command and local audit evidence."""
import hashlib
import json
import time
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import grpc

from .authorization import AccessDenied, require_permission


TABLES = {'settings': 'backend_node_settings_tasks', 'profile': 'backend_operation_tasks',
          'node': 'backend_node_jobs', 'bootstrap': 'backend_node_bootstraps'}


def permitted(actor, kind):
    required = {'maintenance.manage', 'nodes.manage', 'nodes.execute'}
    if kind == 'profile':
        required.add('profiles.manage')
    return required <= actor.principal.scopes


def fresh_admin(conn, actor):
    conn.execute('UPDATE backend_account_guard SET revision=revision+1 WHERE id=1')
    row = conn.execute('UPDATE backend_accounts SET role=role WHERE id=? RETURNING role,status', (actor.account.id,)).fetchone()
    if row is None or row['role'] != 'admin' or row['status'] != 'approved':
        raise AccessDenied('permission_denied')


def spec_for(kind, row):
    intent = json.loads(row['intent_json'])
    if kind == 'settings':
        body = dict(intent, kind='node_settings', command_id=row['id'])
    elif kind == 'profile':
        xray = intent.get('xray') or {}
        body = {'command_id': row['id'], 'protocol': row['protocol'],
                'runtime_name': intent['runtime_name'], 'revision': intent['desired_revision'],
                'action': row['action'], 'uuid': xray.get('uuid', ''),
                'short_id': xray.get('short_id', '')}
    else:
        body = {'action': row['action'], 'intent': {k: v for k, v in intent.items() if k != 'requested_revision'}}
    fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'ticket_id': str(uuid5(NAMESPACE_URL, f"node-plane:force:{row['node_key']}:{kind}:{row['id']}:{fingerprint}")),
            'node_key': row['node_key'], 'command_id': row['id'], 'kind': kind, 'intent': body}


def cancel(db, actor, driver, kind, identity, confirmation):
    if kind not in TABLES or not permitted(actor, kind):
        raise AccessDenied('permission_denied')
    if (not isinstance(confirmation, dict) or confirmation.get('confirm_operation_id') != identity
            or confirmation.get('acknowledge_unknown_outcome') is not True):
        raise AccessDenied('force_confirmation_required', 422)
    try:
        if str(UUID(identity)) != identity:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise AccessDenied('invalid_input', 422) from None
    for scope in ('maintenance.manage', 'nodes.manage', 'nodes.execute'):
        require_permission(actor, scope)
    table, event = TABLES[kind], str(uuid4())
    with db.transaction() as conn:
        from .maintenance_gate import admit
        admit(conn)
        fresh_admin(conn, actor)
        row = conn.execute(f'SELECT * FROM {table} WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise AccessDenied('resource_not_found', 404)
        if row['status'] != 'blocked':
            raise AccessDenied('task_not_blocked', 409)
        actual_kind, actual = kind, row
        if kind == 'bootstrap':
            if row['child_kind'] != 'node-jobs' or not row['child_id']:
                raise AccessDenied('force_operation_unsupported', 409)
            actual_kind = 'node'
            actual = conn.execute('SELECT * FROM backend_node_jobs WHERE id=? AND node_key=?', (row['child_id'], row['node_key'])).fetchone()
            if actual is None or actual['status'] != 'blocked':
                raise AccessDenied('force_operation_unsupported', 409)
        node_key = row['node_key']
        # An operation still running on the controller has not yielded its
        # executor. The HTTP maintenance flock alone must not hide such work.
        for other in ('backend_node_jobs', 'backend_node_settings_tasks', 'backend_operation_tasks', 'backend_agent_rollouts'):
            if conn.execute(f"SELECT 1 FROM {other} WHERE node_key=? AND status='running' LIMIT 1", (node_key,)).fetchone():
                raise AccessDenied('maintenance_busy', 409)
        if conn.execute('SELECT 1 FROM backend_node_drains WHERE node_key=?', (node_key,)).fetchone():
            raise AccessDenied('node_already_draining', 409)
        spec = spec_for(actual_kind, actual)
        frozen = actual['intent_json']
        conn.execute('INSERT INTO backend_recovery_audit VALUES (?,?,?,?,?,?,?,?)',
                     (event, actor.account.id, kind, identity, 'force', 'admitted', 'blocked', datetime.now(timezone.utc).isoformat()))
    actual_table = TABLES[actual_kind]
    try:
        prepared = driver.force_recovery(spec, 'prepare')
        if (set(prepared) != {'ticket_id', 'node_key', 'restart_required'} or type(prepared.get('restart_required')) is not bool
                or (prepared.get('ticket_id'), prepared.get('node_key')) != (spec['ticket_id'], node_key)):
            raise ValueError('invalid force preparation receipt')
        deadline = time.monotonic() + 30
        while True:
            try:
                receipt = driver.force_recovery(spec, 'retire')
                break
            except grpc.RpcError as exc:
                # Restart tears down the channel. Only bounded transport loss,
                # the old instance and inherited mutation lock are retryable.
                retryable = exc.code() in {grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED}
                retryable = retryable or (exc.code() == grpc.StatusCode.FAILED_PRECONDITION and exc.details() in {'force_restart_required', 'force_execution_still_running'})
                if not retryable or time.monotonic() >= deadline:
                    raise
                time.sleep(0.25)
        expected = {k: spec[k] for k in ('ticket_id', 'node_key', 'command_id', 'kind')}
        if receipt != dict(expected, forced=True, outcome_unknown=True) or receipt.get('forced') is not True or receipt.get('outcome_unknown') is not True:
            raise ValueError('invalid force retirement receipt')
        with db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            fresh_admin(conn, actor)
            current = conn.execute(f'SELECT * FROM {actual_table} WHERE id=?', (actual['id'],)).fetchone()
            if current is None or current['status'] != 'blocked' or current['intent_json'] != frozen:
                raise AccessDenied('repair_conflict', 409)
            result = json.dumps({'error_code': 'operator_force_cancelled', 'forced': True, 'outcome_unknown': True,
                                 'ticket_id': spec['ticket_id']}, sort_keys=True)
            conn.execute(f"UPDATE {actual_table} SET status='superseded',result_json=? WHERE id=? AND status='blocked'", (result, actual['id']))
            if kind == 'bootstrap':
                changed = conn.execute("UPDATE backend_node_bootstraps SET status='superseded',error_code='operator_force_cancelled' WHERE id=? AND status='blocked' AND child_id=? RETURNING id", (identity, actual['id'])).fetchone()
                if changed is None:
                    raise AccessDenied('repair_conflict', 409)
            elif actual_kind == 'node':
                # Cancelling a child directly must also release its parent.
                conn.execute("UPDATE backend_node_bootstraps SET status='superseded',error_code='operator_force_cancelled' WHERE child_id=? AND node_key=? AND status='blocked'", (actual['id'], node_key))
            if actual_kind == 'profile':
                from .executor import IntentExecutor
                parent = conn.execute('SELECT profile_id FROM backend_operations WHERE id=?', (actual['operation_id'],)).fetchone()
                old_ops = conn.execute('''SELECT DISTINCT t.operation_id FROM backend_operation_tasks t
                    JOIN backend_operations o ON o.id=t.operation_id WHERE o.profile_id=? AND t.node_key=?
                    AND t.protocol=? AND t.device_id=? AND o.desired_revision<=? AND t.status='awaiting_executor' ''',
                    (parent['profile_id'], node_key, actual['protocol'], actual['device_id'], json.loads(frozen)['desired_revision'])).fetchall()
                for old in old_ops:
                    conn.execute('''UPDATE backend_operation_tasks SET status='superseded'
                        WHERE operation_id=? AND node_key=? AND protocol=? AND device_id=? AND status='awaiting_executor' ''',
                        (old['operation_id'], node_key, actual['protocol'], actual['device_id']))
                    IntentExecutor.refresh_operation(conn, old['operation_id'])
                conn.execute('UPDATE backend_profiles SET desired_revision=desired_revision+1 WHERE id=?', (parent['profile_id'],))
                IntentExecutor.refresh_operation(conn, actual['operation_id'])
                # An unclaimed protocol-removal task may be waiting for this
                # exact revocation. Retire that dependency instead of leaving
                # an unfulfillable queue which prevents the next explicit Apply.
                # Nothing on the agent has been dispatched for these rows.
                waiting = conn.execute("SELECT id,intent_json FROM backend_node_settings_tasks WHERE node_key=? AND status='awaiting_executor'", (node_key,)).fetchall()
                for pending in waiting:
                    removals = json.loads(pending['intent_json']).get('protocol_plan', {}).get('remove_protocols', [])
                    if actual['protocol'] in removals:
                        conn.execute("UPDATE backend_node_settings_tasks SET status='superseded',result_json=? WHERE id=? AND status='awaiting_executor'",
                                     (json.dumps({'error_code': 'operator_force_cancelled_dependency', 'cancelled_task_id': actual['id']}, sort_keys=True), pending['id']))
            else:
                revision = actual['revision']
                conn.execute('UPDATE backend_nodes SET desired_revision=CASE WHEN desired_revision>? THEN desired_revision+1 ELSE ? END WHERE key=?', (revision, revision+1, node_key))
            conn.execute("UPDATE backend_recovery_audit SET outcome='force_cancelled',status='superseded' WHERE id=?", (event,))
    except Exception as exc:
        with db.transaction() as conn:
            conn.execute("UPDATE backend_recovery_audit SET outcome='unconfirmed' WHERE id=?", (event,))
        if isinstance(exc, AccessDenied):
            raise
        if isinstance(exc, grpc.RpcError):
            if exc.code() == grpc.StatusCode.UNIMPLEMENTED:
                raise AccessDenied('force_agent_upgrade_required', 409) from None
            if exc.code() in {grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED}:
                raise AccessDenied('force_agent_unavailable', 409) from None
            reason = exc.details()
            if reason in {'force_restart_required', 'force_execution_still_running',
                          'force_managed_restart_unavailable', 'force_restart_unavailable',
                          'force_command_conflict', 'force_ticket_conflict',
                          'force_agent_removal_pending', 'force_command_already_terminal'}:
                raise AccessDenied(reason, 409) from None
        raise AccessDenied('force_recovery_unconfirmed', 409) from None
    return {'id': identity, 'kind': kind, 'status': 'superseded', 'outcome': 'force_cancelled',
            'replacement_id': None, 'observation': {'forced': True, 'outcome_unknown': True}}


def forced_profile_targets(conn, node_key, protocols=None):
    """Latest exact forced cancellations which still need explicit resync."""
    rows = conn.execute("""SELECT t.*,o.profile_id FROM backend_operation_tasks t
        JOIN backend_operations o ON o.id=t.operation_id WHERE t.node_key=?
        AND t.status='superseded' AND NOT EXISTS (
            SELECT 1 FROM backend_operation_tasks n JOIN backend_operations no ON no.id=n.operation_id
            WHERE n.node_key=t.node_key AND n.protocol=t.protocol AND n.device_id=t.device_id
            AND no.profile_id=o.profile_id AND no.desired_revision>o.desired_revision)""", (node_key,)).fetchall()
    result = {}
    for row in rows:
        if protocols is not None and row['protocol'] not in protocols:
            continue
        try:
            value = json.loads(row['result_json'] or '{}')
        except (ValueError, TypeError):
            continue
        if value.get('error_code') == 'operator_force_cancelled' and value.get('forced') is True:
            result.setdefault(row['profile_id'], set()).add((node_key, row['protocol']))
    return result


def resume_forced_profile_targets(conn, actor, node_key):
    """An explicit apply/reconcile retries desired access with fresh identities."""
    from .operations import OperationRepository
    for profile_id, targets in sorted(forced_profile_targets(conn, node_key).items()):
        conn.execute('UPDATE backend_profiles SET desired_revision=desired_revision+1 WHERE id=?', (profile_id,))
        OperationRepository.record(conn, actor, profile_id, targets, scoped_targets=targets)
