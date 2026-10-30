"""Durable scoped protocol removal revocation flow.

Revokes materialized grants and schedules temporary config deletions for removed
protocols before node runtime cleanup.
"""
from __future__ import annotations

import json
from .authorization import require_permission
from .operations import OperationRepository
from .temporary_configs import TemporaryConfigService


def is_revocation_confirmed(conn, node_key: str, remove_protocols: list[str]) -> bool:
    """Check if all required revocations for removed protocols are confirmed."""
    if not remove_protocols:
        return True
    placeholders = ','.join('?' for _ in remove_protocols)

    # 1. No active backend_grants for removed protocols on this node
    if conn.execute(f'''
        SELECT 1 FROM backend_grants
        WHERE node_key = ? AND protocol IN ({placeholders})
        LIMIT 1
    ''', (node_key, *remove_protocols)).fetchone():
        return False

    # 2. No pending, running, or blocked operation tasks for removed protocols on this node
    if conn.execute(f'''
        SELECT 1 FROM backend_operation_tasks t
        WHERE t.node_key = ? AND t.protocol IN ({placeholders})
          AND t.status IN ('awaiting_executor', 'running', 'blocked')
        LIMIT 1
    ''', (node_key, *remove_protocols)).fetchone():
        return False

    # 3. Latest tasks for affected historical profile/device targets must be 'delete' and 'succeeded'
    latest = conn.execute(f'''
        SELECT t.action, t.status FROM backend_operation_tasks t
        JOIN backend_operations o ON o.id = t.operation_id
        WHERE t.node_key = ? AND t.protocol IN ({placeholders})
          AND NOT EXISTS (
              SELECT 1 FROM backend_operation_tasks newer
              JOIN backend_operations no ON no.id = newer.operation_id
              WHERE newer.node_key = t.node_key AND newer.protocol = t.protocol
                AND newer.device_id = t.device_id
                AND no.profile_id = o.profile_id AND no.desired_revision > o.desired_revision
          )
    ''', (node_key, *remove_protocols)).fetchall()
    if not all(r['action'] == 'delete' and r['status'] == 'succeeded' for r in latest):
        return False

    # 4. Relevant temporary configs must be terminal ('expired', 'revoked', 'cancelled')
    if conn.execute(f'''
        SELECT 1 FROM backend_temporary_configs
        WHERE node_key = ? AND protocol IN ({placeholders})
          AND status NOT IN ('expired', 'revoked', 'cancelled')
        LIMIT 1
    ''', (node_key, *remove_protocols)).fetchone():
        return False

    return True


def revoke_removed_protocols(conn, actor, node_key: str, removed_protocols: list[str]) -> list[str]:
    """Execute durable scoped revocation for removed protocols on node_key."""
    if not removed_protocols:
        return []

    # Protocol removal revokes grants, requiring grants.manage permission
    require_permission(actor, 'grants.manage')

    placeholders = ','.join('?' for _ in removed_protocols)

    # Prune removed explicit node/protocol entries from saved per-profile policies
    # (members and admins' preserved member policies); leave region/all rules intact.
    for row in conn.execute('SELECT profile_id, explicit_json FROM backend_grant_policies').fetchall():
        explicit = json.loads(row['explicit_json'])
        new_explicit = [
            g for g in explicit
            if not (g.get('node_key') == node_key and g.get('protocol') in removed_protocols)
        ]
        if len(new_explicit) != len(explicit):
            conn.execute(
                'UPDATE backend_grant_policies SET explicit_json = ? WHERE profile_id = ?',
                (json.dumps(new_explicit, sort_keys=True), row['profile_id'])
            )

    # Find all affected profiles (current grants or historical operation tasks for removed protocols)
    affected_rows = conn.execute(f'''
        SELECT DISTINCT profile_id FROM backend_grants
        WHERE node_key = ? AND protocol IN ({placeholders})
        UNION
        SELECT DISTINCT o.profile_id FROM backend_operation_tasks t
        JOIN backend_operations o ON o.id = t.operation_id
        WHERE t.node_key = ? AND t.protocol IN ({placeholders})
    ''', (node_key, *removed_protocols, node_key, *removed_protocols)).fetchall()
    affected_profiles = [r['profile_id'] for r in affected_rows]

    # Pre-collect historical targets for affected profiles so they include the removed protocol
    profile_targets = {}
    for profile_id in affected_profiles:
        targets = OperationRepository.targets(conn, profile_id)
        targets.update((node_key, p) for p in removed_protocols)
        profile_targets[profile_id] = targets

    # Revoke all materialized grants for removed node/protocol including admins
    conn.execute(f'''
        DELETE FROM backend_grants
        WHERE node_key = ? AND protocol IN ({placeholders})
    ''', (node_key, *removed_protocols))

    scoped_targets = {(node_key, p) for p in removed_protocols}
    operation_ids = []

    for profile_id in sorted(affected_profiles):
        # Retire old queued ensures so they cannot execute after newer revocation intent
        affected_op_ids = [r['operation_id'] for r in conn.execute(f'''
            SELECT DISTINCT operation_id FROM backend_operation_tasks
            WHERE node_key = ? AND protocol IN ({placeholders})
              AND action = 'ensure' AND status = 'awaiting_executor'
              AND operation_id IN (SELECT id FROM backend_operations WHERE profile_id = ?)
        ''', (node_key, *removed_protocols, profile_id)).fetchall()]

        if affected_op_ids:
            conn.execute(f'''
                UPDATE backend_operation_tasks SET status = 'superseded'
                WHERE node_key = ? AND protocol IN ({placeholders})
                  AND action = 'ensure' AND status = 'awaiting_executor'
                  AND operation_id IN (SELECT id FROM backend_operations WHERE profile_id = ?)
            ''', (node_key, *removed_protocols, profile_id))

            for op_id in affected_op_ids:
                states = {r['status'] for r in conn.execute(
                    'SELECT status FROM backend_operation_tasks WHERE operation_id = ?', (op_id,)
                ).fetchall()}
                status = ('blocked' if 'blocked' in states else 'running' if 'running' in states else
                          'awaiting_executor' if 'awaiting_executor' in states else
                          'superseded' if 'superseded' in states else 'succeeded')
                conn.execute('UPDATE backend_operations SET status = ? WHERE id = ?', (status, op_id))

        conn.execute('UPDATE backend_profiles SET desired_revision = desired_revision + 1 WHERE id = ?', (profile_id,))
        op = OperationRepository.record(conn, actor, profile_id, profile_targets[profile_id], scoped_targets=scoped_targets)
        operation_ids.append(op['id'])

    # Schedule affected temporary configs through existing revocation contracts
    temp_rows = conn.execute(f'''
        SELECT * FROM backend_temporary_configs
        WHERE node_key = ? AND protocol IN ({placeholders})
          AND status NOT IN ('expired', 'revoked', 'cancelled')
    ''', (node_key, *removed_protocols)).fetchall()

    for temp in temp_rows:
        t_status = temp['status']
        if t_status == 'queued':
            conn.execute(
                "UPDATE backend_temporary_configs SET status = 'cancelled', revoke_reason = 'manual', error_code = NULL WHERE id = ?",
                (temp['id'],)
            )
            TemporaryConfigService.event(conn, temp, 'revoke', 'cancelled', actor)
        elif t_status in {'active', 'blocked', 'issuing'}:
            conn.execute(
                "UPDATE backend_temporary_configs SET status = 'revoking', revoke_reason = 'manual', error_code = NULL WHERE id = ?",
                (temp['id'],)
            )
            TemporaryConfigService.event(conn, temp, 'revoke', 'revoking', actor)
        elif t_status == 'revoke_blocked':
            # Preserve existing revoke_blocked state: do not reset into unguarded replay
            pass

    return operation_ids
