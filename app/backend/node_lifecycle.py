"""Trusted first phase of backend-owned node retirement.

The node stays in the registry while its profiles are revoked. Runtime and
agent cleanup require a separate, verifiable contract before final deletion.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from uuid import uuid4

from .authorization import AccessDenied, require_permission
from .operations import OperationRepository


class NodeLifecycle:
    def __init__(self, db):
        self.db = db

    def initialize_schema(self):
        from .node_settings import NodeSettingsService
        NodeSettingsService(self.db).initialize_schema()
        with self.db.transaction() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_node_drains (
                node_key TEXT PRIMARY KEY REFERENCES backend_nodes(key),
                actor_id TEXT NOT NULL REFERENCES backend_accounts(id),
                operation_ids_json TEXT NOT NULL,
                started_at TEXT NOT NULL
            )''')
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_node_cleanup (
                node_key TEXT PRIMARY KEY REFERENCES backend_nodes(key),
                actor_id TEXT NOT NULL REFERENCES backend_accounts(id),
                command_id TEXT NOT NULL UNIQUE,
                phase TEXT NOT NULL CHECK(phase IN (
                    'preparing', 'prepared', 'runtime_deleted',
                    'uninstall_uncertain', 'uninstall_scheduled')),
                started_at TEXT NOT NULL
            )''')
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_node_retirements (
                node_key TEXT PRIMARY KEY,
                actor_id TEXT NOT NULL REFERENCES backend_accounts(id),
                mode TEXT NOT NULL CHECK(mode IN ('verified', 'registry_only')),
                reason TEXT NOT NULL,
                previous_cleanup_phase TEXT,
                unfinished_tasks INTEGER NOT NULL,
                evidence_json TEXT,
                retired_at TEXT NOT NULL
            )''')
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_node_verification_targets (
                node_key TEXT PRIMARY KEY REFERENCES backend_nodes(key),
                target TEXT NOT NULL
            )''')
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_node_host_identities (
                node_key TEXT PRIMARY KEY REFERENCES backend_nodes(key),
                fingerprint TEXT NOT NULL
            )''')
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_node_removal_inventories (
                node_key TEXT PRIMARY KEY REFERENCES backend_nodes(key),
                resources_json TEXT NOT NULL
            )''')

    def bind_verification_target(self, actor, node_key, verifier):
        """Bind an active agent and its host fingerprint before draining."""
        require_permission(actor, 'maintenance.manage')
        target = 'local' if verifier.local else verifier.ssh_target
        fingerprint = verifier.capture_identity(node_key)
        resources = verifier.capture_resources(node_key, fingerprint)
        from .removal_inventory import validate_inventory
        encoded = json.dumps(validate_inventory(resources), sort_keys=True)
        with self.db.transaction() as conn:
            node = conn.execute('''UPDATE backend_nodes SET enabled = enabled
                WHERE key = ? RETURNING enabled''', (node_key,)).fetchone()
            if node is None:
                raise AccessDenied('resource_not_found', 404)
            if conn.execute('SELECT 1 FROM backend_node_drains WHERE node_key = ?', (node_key,)).fetchone():
                raise AccessDenied('node_already_draining', 409)
            old = conn.execute('SELECT target FROM backend_node_verification_targets WHERE node_key = ?', (node_key,)).fetchone()
            if old is not None:
                if old['target'] != target:
                    raise AccessDenied('verification_target_conflict', 409)
            identity = conn.execute('SELECT fingerprint FROM backend_node_host_identities WHERE node_key = ?', (node_key,)).fetchone()
            if identity is not None and identity['fingerprint'] != fingerprint:
                raise AccessDenied('verification_identity_conflict', 409)
            if old is None:
                conn.execute('INSERT INTO backend_node_verification_targets(node_key, target) VALUES (?, ?)', (node_key, target))
            if identity is None:
                conn.execute('INSERT INTO backend_node_host_identities(node_key, fingerprint) VALUES (?, ?)', (node_key, fingerprint))
            inventory = conn.execute('SELECT resources_json FROM backend_node_removal_inventories WHERE node_key=?', (node_key,)).fetchone()
            if inventory is not None and inventory['resources_json'] != encoded:
                raise AccessDenied('verification_inventory_conflict', 409)
            if inventory is None:
                conn.execute('INSERT INTO backend_node_removal_inventories VALUES (?,?)', (node_key, encoded))
            return {'node_key': node_key, 'target': target, 'host_fingerprint': fingerprint}

    def start_drain(self, actor, node_key, *, abandon_uncertain_settings=False):
        """Atomically stop new grants and queue revocation of every known target.

        Callers must hold the same exclusive lock as backend.executor. A worker
        already running cannot finish an old ensure after this transaction.
        """
        require_permission(actor, 'maintenance.manage')
        if not isinstance(node_key, str) or not node_key or len(node_key) > 128:
            raise AccessDenied('invalid_input', 422)
        with self.db.transaction() as conn:
            node = conn.execute('SELECT key FROM backend_nodes WHERE key = ?', (node_key,)).fetchone()
            if node is None:
                raise AccessDenied('resource_not_found', 404)
            previous = conn.execute('SELECT operation_ids_json FROM backend_node_drains WHERE node_key = ?', (node_key,)).fetchone()
            if previous is not None:
                return {'node_key': node_key, 'status': 'draining',
                        'operation_ids': json.loads(previous['operation_ids_json'])}
            if conn.execute('''SELECT 1 FROM backend_agent_rollouts WHERE node_key = ?
                AND status IN ('awaiting_executor', 'running')''',
                (node_key,)).fetchone():
                raise AccessDenied('agent_rollout_uncertain', 409)
            if not abandon_uncertain_settings and conn.execute('''SELECT 1 FROM backend_node_settings_tasks WHERE node_key = ?
                AND status IN ('running', 'blocked')''', (node_key,)).fetchone():
                raise AccessDenied('node_settings_uncertain', 409)
            if not abandon_uncertain_settings and conn.execute("SELECT 1 FROM backend_node_jobs WHERE node_key = ? AND status IN ('running', 'blocked')", (node_key,)).fetchone():
                raise AccessDenied('node_settings_uncertain', 409)
            conn.execute("UPDATE backend_node_jobs SET status = 'superseded' WHERE node_key = ? AND status IN ('awaiting_executor', 'running', 'blocked')" if abandon_uncertain_settings else
                         "UPDATE backend_node_jobs SET status = 'superseded' WHERE node_key = ? AND status = 'awaiting_executor'", (node_key,))
            conn.execute("""UPDATE backend_node_settings_tasks SET status = 'superseded' WHERE node_key = ?
                AND status IN ('awaiting_executor', 'running', 'blocked')""" if abandon_uncertain_settings else
                "UPDATE backend_node_settings_tasks SET status = 'superseded' WHERE node_key = ? AND status = 'awaiting_executor'", (node_key,))
            conn.execute('UPDATE backend_nodes SET enabled = 0 WHERE key = ?', (node_key,))
            # Include historical targets even when their grant was removed: an
            # earlier ensure may have run, timed out, or remained queued.
            profiles = conn.execute('''SELECT DISTINCT profile_id FROM backend_grants WHERE node_key = ?
                UNION SELECT DISTINCT o.profile_id FROM backend_operation_tasks t
                    JOIN backend_operations o ON o.id = t.operation_id WHERE t.node_key = ?''',
                (node_key, node_key)).fetchall()
            operation_ids = []
            for item in profiles:
                profile_id = item['profile_id']
                previous_targets = OperationRepository.targets(conn, profile_id)
                conn.execute('DELETE FROM backend_grants WHERE profile_id = ? AND node_key = ?', (profile_id, node_key))
                conn.execute('UPDATE backend_profiles SET desired_revision = desired_revision + 1 WHERE id = ?', (profile_id,))
                operation = OperationRepository.record(conn, actor, profile_id, previous_targets)
                operation_ids.append(operation['id'])
            conn.execute('''INSERT INTO backend_node_drains(node_key, actor_id, operation_ids_json, started_at)
                VALUES (?, ?, ?, ?)''', (node_key, actor.account.id,
                    json.dumps(operation_ids), datetime.now(timezone.utc).isoformat()))
            return {'node_key': node_key, 'status': 'draining', 'operation_ids': operation_ids}

    def drain_status(self, actor, node_key):
        require_permission(actor, 'maintenance.manage')
        with self.db.connect() as conn:
            drain = conn.execute('SELECT operation_ids_json FROM backend_node_drains WHERE node_key = ?', (node_key,)).fetchone()
            if drain is None:
                raise AccessDenied('resource_not_found', 404)
            pending, blocked, ready = self._revocation_state(conn, node_key)
            operation_ids = json.loads(drain['operation_ids_json'])
            cleanup = conn.execute('SELECT phase FROM backend_node_cleanup WHERE node_key = ?', (node_key,)).fetchone()
            return {'node_key': node_key, 'status': 'draining',
                    'operation_ids': operation_ids, 'pending_tasks': pending,
                    'blocked_tasks': blocked, 'revocations_complete': ready,
                    'cleanup_phase': cleanup['phase'] if cleanup else None}

    def overview(self, actor, node_key):
        require_permission(actor, 'maintenance.manage')
        with self.db.connect() as conn:
            node = conn.execute('SELECT key FROM backend_nodes WHERE key = ?',
                                (node_key,)).fetchone()
            if node is None:
                raise AccessDenied('resource_not_found', 404)
            drain = conn.execute('SELECT 1 FROM backend_node_drains WHERE node_key = ?',
                                 (node_key,)).fetchone()
            bound = conn.execute('''SELECT target FROM backend_node_verification_targets
                WHERE node_key = ?''', (node_key,)).fetchone()
        if drain is not None:
            return {**self.drain_status(actor, node_key),
                    'verification_target': bound['target'] if bound else None}
        return {'node_key': node_key, 'status': 'active', 'operation_ids': [],
                'pending_tasks': 0, 'blocked_tasks': 0,
                'revocations_complete': False, 'cleanup_phase': None,
                'verification_target': bound['target'] if bound else None}

    @staticmethod
    def _revocation_state(conn, node_key):
        rows = conn.execute('''SELECT t.status, t.action FROM backend_operation_tasks t
            WHERE t.node_key = ? ORDER BY t.id''', (node_key,)).fetchall()
        pending = sum(row['status'] in {'awaiting_executor', 'running'} for row in rows)
        blocked = sum(row['status'] == 'blocked' for row in rows)
        # Every profile ever targeted by this node received a fresh delete
        # intent at drain start. A later profile edit may create another;
        # both remain visible in the task states above.
        latest = conn.execute('''SELECT t.action, t.status FROM backend_operation_tasks t
            JOIN backend_operations o ON o.id = t.operation_id
            WHERE t.node_key = ? AND NOT EXISTS (
                SELECT 1 FROM backend_operation_tasks newer
                JOIN backend_operations no ON no.id = newer.operation_id
                WHERE newer.node_key = t.node_key AND newer.protocol = t.protocol
                  AND no.profile_id = o.profile_id AND no.desired_revision > o.desired_revision
            )''', (node_key,)).fetchall()
        ready = pending == 0 and blocked == 0 and all(
            row['action'] == 'delete' and row['status'] == 'succeeded' for row in latest)
        return pending, blocked, ready

    def cleanup(self, actor, node_key, driver, *, expected_phase=None):
        """Advance one verifiable remote phase under the worker's file lock.

        An ambiguous uninstall cannot be retried automatically: the agent may
        already be gone. The registry stays intact for external verification.
        """
        require_permission(actor, 'maintenance.manage')
        with self.db.transaction() as conn:
            node = conn.execute('''UPDATE backend_nodes SET enabled = enabled
                WHERE key = ? RETURNING key''', (node_key,)).fetchone()
            if node is None or conn.execute('SELECT 1 FROM backend_node_drains WHERE node_key = ?', (node_key,)).fetchone() is None:
                raise AccessDenied('resource_not_found', 404)
            if not self._revocation_state(conn, node_key)[2]:
                raise AccessDenied('node_revocations_incomplete', 409)
            row = conn.execute('SELECT command_id, phase FROM backend_node_cleanup WHERE node_key = ?', (node_key,)).fetchone()
            if row is None:
                command_id = str(uuid4())
                phase = 'preparing'
                conn.execute('''INSERT INTO backend_node_cleanup(node_key, actor_id, command_id, phase, started_at)
                    VALUES (?, ?, ?, ?, ?)''', (node_key, actor.account.id, command_id, phase,
                    datetime.now(timezone.utc).isoformat()))
            else:
                command_id, phase = row['command_id'], row['phase']
            if expected_phase is not None:
                allowed = (phase == expected_phase or
                           expected_phase == 'not_started' and phase == 'preparing')
                if not allowed:
                    return {'node_key': node_key, 'phase': phase,
                            'command_id': command_id}
        if phase in {'uninstall_uncertain', 'uninstall_scheduled'}:
            return {'node_key': node_key, 'phase': phase, 'command_id': command_id}
        if phase == 'preparing':
            driver.decommission(node_key, command_id, 'prepare')
            next_phase = 'prepared'
        elif phase == 'prepared':
            driver.decommission(node_key, command_id, 'delete_runtime')
            next_phase = 'runtime_deleted'
        else:
            # Commit uncertainty before the RPC: if the agent disappears, a
            # timeout cannot prove whether systemd scheduled its uninstall.
            with self.db.transaction() as conn:
                changed = conn.execute("""UPDATE backend_node_cleanup SET phase = 'uninstall_uncertain'
                    WHERE node_key = ? AND phase = 'runtime_deleted' RETURNING node_key""", (node_key,)).fetchone()
                if changed is None:
                    raise AccessDenied('cleanup_phase_conflict', 409)
            driver.decommission(node_key, command_id, 'uninstall')
            next_phase = 'uninstall_scheduled'
        with self.db.transaction() as conn:
            changed = conn.execute('''UPDATE backend_node_cleanup SET phase = ?
                WHERE node_key = ? AND command_id = ? AND phase = ? RETURNING node_key''',
                (next_phase, node_key, command_id,
                 'uninstall_uncertain' if next_phase == 'uninstall_scheduled' else phase)).fetchone()
            if changed is None:
                raise AccessDenied('cleanup_phase_conflict', 409)
        return {'node_key': node_key, 'phase': next_phase, 'command_id': command_id}

    def retire_registry_only(self, actor, node_key, reason):
        """Explicitly abandon remote certainty while removing backend state.

        Callers must hold the same worker lock as drain/cleanup. The permanent
        tombstone prevents stale historical targets from being dispatched and
        prevents reuse of the old node key.
        """
        require_permission(actor, 'maintenance.manage')
        if not isinstance(reason, str) or not 20 <= len(reason.strip()) <= 500:
            raise AccessDenied('retirement_reason_required', 422)
        reason = reason.strip()
        with self.db.transaction() as conn:
            existing = conn.execute('SELECT mode, reason, unfinished_tasks FROM backend_node_retirements WHERE node_key = ?', (node_key,)).fetchone()
            if existing is not None:
                if existing['mode'] != 'registry_only' or existing['reason'] != reason:
                    raise AccessDenied('retirement_conflict', 409)
                return {'node_key': node_key, 'mode': existing['mode'],
                        'unfinished_tasks': existing['unfinished_tasks']}
        # Safe without agent contact: the operator explicitly accepts that
        # remote profiles, containers, or a delayed command may still exist.
        self.start_drain(actor, node_key, abandon_uncertain_settings=True)
        with self.db.transaction() as conn:
            node = conn.execute('''UPDATE backend_nodes SET enabled = 0
                WHERE key = ? RETURNING key''', (node_key,)).fetchone()
            if node is None:
                raise AccessDenied('resource_not_found', 404)
            cleanup = conn.execute('SELECT phase FROM backend_node_cleanup WHERE node_key = ?', (node_key,)).fetchone()
            affected = conn.execute('''SELECT DISTINCT operation_id FROM backend_operation_tasks
                WHERE node_key = ? AND status IN ('awaiting_executor', 'running', 'blocked')''', (node_key,)).fetchall()
            changed = conn.execute('''UPDATE backend_operation_tasks SET status = 'superseded'
                WHERE node_key = ? AND status IN ('awaiting_executor', 'running', 'blocked') RETURNING id''', (node_key,)).fetchall()
            from .executor import IntentExecutor
            executor = IntentExecutor(self.db, None)
            for item in affected:
                executor.refresh_operation(conn, item['operation_id'])
            conn.execute('''INSERT INTO backend_node_retirements(
                node_key, actor_id, mode, reason, previous_cleanup_phase,
                unfinished_tasks, evidence_json, retired_at)
                VALUES (?, ?, 'registry_only', ?, ?, ?, NULL, ?)''',
                (node_key, actor.account.id, reason,
                 cleanup['phase'] if cleanup else None, len(changed),
                 datetime.now(timezone.utc).isoformat()))
            conn.execute('DELETE FROM backend_node_cleanup WHERE node_key = ?', (node_key,))
            conn.execute('DELETE FROM backend_node_drains WHERE node_key = ?', (node_key,))
            conn.execute('DELETE FROM backend_node_verification_targets WHERE node_key = ?', (node_key,))
            conn.execute('DELETE FROM backend_node_host_identities WHERE node_key = ?', (node_key,))
            conn.execute('DELETE FROM backend_node_removal_inventories WHERE node_key = ?', (node_key,))
            conn.execute("UPDATE backend_node_removals SET status = 'abandoned' WHERE node_key = ?", (node_key,))
            from .alerts import AlertService
            AlertService.retire_node(conn, node_key)
            conn.execute('DELETE FROM backend_nodes WHERE key = ?', (node_key,))
            return {'node_key': node_key, 'mode': 'registry_only',
                    'unfinished_tasks': len(changed)}

    def retire_verified(self, actor, node_key, verifier):
        """Remove the registry record only after independent host inspection.

        The caller holds the worker lock. Verification runs outside the DB
        transaction, then all safety predicates are rechecked before deletion.
        """
        require_permission(actor, 'maintenance.manage')
        with self.db.connect() as conn:
            prior = conn.execute('SELECT mode FROM backend_node_retirements WHERE node_key = ?', (node_key,)).fetchone()
            if prior is not None:
                if prior['mode'] != 'verified':
                    raise AccessDenied('retirement_conflict', 409)
                return {'node_key': node_key, 'mode': 'verified'}
            cleanup = conn.execute('SELECT phase FROM backend_node_cleanup WHERE node_key = ?', (node_key,)).fetchone()
            if cleanup is None or cleanup['phase'] not in {'uninstall_uncertain', 'uninstall_scheduled'}:
                raise AccessDenied('agent_uninstall_not_requested', 409)
            bound = conn.execute('SELECT target FROM backend_node_verification_targets WHERE node_key = ?', (node_key,)).fetchone()
            identity = conn.execute('SELECT fingerprint FROM backend_node_host_identities WHERE node_key = ?', (node_key,)).fetchone()
            requested_target = 'local' if verifier.local else verifier.ssh_target
            if bound is None or bound['target'] != requested_target or identity is None:
                raise AccessDenied('verification_target_mismatch', 409)
            inventory = conn.execute('SELECT resources_json FROM backend_node_removal_inventories WHERE node_key=?', (node_key,)).fetchone()
            if inventory is None:
                raise AccessDenied('verification_inventory_required', 409)
            resources = json.loads(inventory['resources_json'])
        evidence = verifier.verify(node_key, identity['fingerprint'], resources)
        from .removal_inventory import inventory_digest
        if (not isinstance(evidence, dict) or evidence.get('result') != 'agent_and_standard_artifacts_absent'
                or evidence.get('inventory_digest') != inventory_digest(resources)
                or evidence.get('method') not in {'local', 'ssh'}
                or not isinstance(evidence.get('target'), str)
                or evidence.get('host_fingerprint') != identity['fingerprint']
                or not isinstance(evidence.get('checked_at'), str)):
            raise ValueError('invalid host verification evidence')
        with self.db.transaction() as conn:
            node = conn.execute('''UPDATE backend_nodes SET enabled = 0
                WHERE key = ? RETURNING key''', (node_key,)).fetchone()
            if node is None:
                raise AccessDenied('resource_not_found', 404)
            cleanup = conn.execute('SELECT phase FROM backend_node_cleanup WHERE node_key = ?', (node_key,)).fetchone()
            if cleanup is None or cleanup['phase'] not in {'uninstall_uncertain', 'uninstall_scheduled'}:
                raise AccessDenied('cleanup_phase_conflict', 409)
            if not self._revocation_state(conn, node_key)[2]:
                raise AccessDenied('node_revocations_incomplete', 409)
            bound = conn.execute('SELECT target FROM backend_node_verification_targets WHERE node_key = ?', (node_key,)).fetchone()
            identity = conn.execute('SELECT fingerprint FROM backend_node_host_identities WHERE node_key = ?', (node_key,)).fetchone()
            if (bound is None or bound['target'] != evidence['target'] or identity is None
                    or identity['fingerprint'] != evidence['host_fingerprint']):
                raise AccessDenied('verification_target_mismatch', 409)
            conn.execute('''INSERT INTO backend_node_retirements(
                node_key, actor_id, mode, reason, previous_cleanup_phase,
                unfinished_tasks, evidence_json, retired_at)
                VALUES (?, ?, 'verified', ?, ?, 0, ?, ?)''',
                (node_key, actor.account.id, 'independent host verification passed',
                 cleanup['phase'], json.dumps(evidence, sort_keys=True),
                 datetime.now(timezone.utc).isoformat()))
            conn.execute('DELETE FROM backend_node_cleanup WHERE node_key = ?', (node_key,))
            conn.execute('DELETE FROM backend_node_drains WHERE node_key = ?', (node_key,))
            conn.execute('DELETE FROM backend_node_verification_targets WHERE node_key = ?', (node_key,))
            conn.execute('DELETE FROM backend_node_host_identities WHERE node_key = ?', (node_key,))
            conn.execute('DELETE FROM backend_node_removal_inventories WHERE node_key = ?', (node_key,))
            from .alerts import AlertService
            AlertService.retire_node(conn, node_key)
            conn.execute('DELETE FROM backend_nodes WHERE key = ?', (node_key,))
            conn.execute("UPDATE backend_node_removals SET status = 'succeeded', error_code = NULL WHERE node_key = ?", (node_key,))
        return {'node_key': node_key, 'mode': 'verified'}
