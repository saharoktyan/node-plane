"""Single-worker durable outbox execution.

Use the local CLI under an exclusive file lock. A claimed task is never
automatically retried after a crash: uncertain outcomes block its whole node.
This is deliberately conservative until reconciliation is implemented.
"""
from datetime import datetime, timezone
import argparse
import fcntl
import json
import os
from .authorization import AccessDenied, require_permission
from .operations import OperationRepository
from .node_settings import NodeSettingsExecutor
from .config_issuance import ConfigIssuanceService
from .agent_rollout import AgentRolloutService


class IntentExecutor:
    def __init__(self, db, driver):
        self.db, self.driver = db, driver

    def recover(self):
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_operation_tasks SET status = 'blocked' WHERE status = 'running'")
            conn.execute("UPDATE backend_operations SET status = 'blocked' WHERE status = 'running'")

    def queue_expired_profiles(self):
        """Turn reached expiries into durable revocations once per revision.

        Keep grants and identities, so extending the expiry can restore access.
        Reuse the account that requested the expiring revision for audit attribution;
        no delegated permission or remote command runs inside this transaction.
        """
        from .authorization import Account, Actor, Principal, PrincipalKind
        from .maintenance_gate import active
        timestamp = datetime.now(timezone.utc)
        queued = 0
        with self.db.transaction() as conn:
            conn.execute('UPDATE backend_account_guard SET revision = revision + 1 WHERE id = 1')
            if active(conn):
                return 0
            from .profile_commands import ProfileCommands
            ProfileCommands.restore_admin_expiries(conn)
            rows = conn.execute("""SELECT p.id, p.desired_revision, p.expires_at,
                o.actor_id FROM backend_profiles p JOIN backend_operations o
                ON o.profile_id = p.id AND o.desired_revision = p.desired_revision
                WHERE p.frozen = 0 AND p.expires_at IS NOT NULL AND p.expires_at <= ?
                AND NOT EXISTS (SELECT 1 FROM backend_profile_deletions d WHERE d.profile_id = p.id)
                AND EXISTS (SELECT 1 FROM backend_operation_tasks t
                    WHERE t.operation_id = o.id AND t.action = 'ensure')
                ORDER BY p.expires_at, p.id LIMIT 100""", (timestamp.isoformat(),)).fetchall()
            for row in rows:
                if datetime.fromisoformat(row['expires_at']) > timestamp:
                    continue
                changed = conn.execute("""UPDATE backend_profiles
                    SET desired_revision = desired_revision + 1
                    WHERE id = ? AND desired_revision = ? RETURNING id""",
                    (row['id'], row['desired_revision'])).fetchone()
                if changed is None:
                    continue
                actor = Actor(Principal('scheduled-profile-expiry', PrincipalKind.ACCOUNT,
                    frozenset(), row['actor_id']), Account(row['actor_id']))
                OperationRepository.record(conn, actor, row['id'],
                    OperationRepository.targets(conn, row['id']))
                queued += 1
        return queued

    def refresh_operation(self, conn, operation_id):
        states = {r['status'] for r in conn.execute('SELECT status FROM backend_operation_tasks WHERE operation_id = ?', (operation_id,)).fetchall()}
        status = ('blocked' if 'blocked' in states else 'running' if 'running' in states else
                  'awaiting_executor' if 'awaiting_executor' in states else
                  'superseded' if 'superseded' in states else 'succeeded')
        conn.execute('UPDATE backend_operations SET status = ? WHERE id = ?', (status, operation_id))
        from .devices import DeviceRepository
        DeviceRepository.settle_deletions(conn)

    def resolve_blocked(self, actor, task_id):
        """Trusted admin repair after agent restart; serialized by worker flock.

        Remote retirement comes first. A failed DB commit can safely retry the
        same agent RPC, which returns its durable observation without mutation.
        """
        require_permission(actor, 'maintenance.manage')
        with self.db.connect() as conn:
            row = conn.execute("""SELECT t.*, o.profile_id FROM backend_operation_tasks t
                JOIN backend_operations o ON o.id = t.operation_id WHERE t.id = ?""", (task_id,)).fetchone()
            if row is None:
                raise AccessDenied('resource_not_found', 404)
            replay = conn.execute('SELECT operation_id, observation_json FROM backend_repairs WHERE task_id = ?', (task_id,)).fetchone()
            if replay:
                return {'operation_id': replay['operation_id'], 'observation': json.loads(replay['observation_json'])}
            if row['status'] != 'blocked':
                raise AccessDenied('task_not_blocked', 409)
            intent = json.loads(row['intent_json'])
        observation = self.driver.resolve(task_id, intent)
        expected = {'disk_present', 'live_present', 'identity_matches', 'config_available'}
        if set(observation) != expected or any(type(value) is not bool for value in observation.values()):
            raise ValueError('invalid repair observation')
        with self.db.transaction() as conn:
            current = conn.execute('SELECT status FROM backend_operation_tasks WHERE id = ?', (task_id,)).fetchone()
            if current is None or current['status'] != 'blocked':
                raise AccessDenied('repair_conflict', 409)
            affected = conn.execute("""SELECT DISTINCT t.operation_id FROM backend_operation_tasks t
                JOIN backend_operations o ON o.id = t.operation_id
                WHERE o.profile_id = ? AND t.node_key = ? AND t.status IN ('awaiting_executor', 'blocked')""",
                (row['profile_id'], row['node_key'])).fetchall()
            conn.execute("""UPDATE backend_operation_tasks SET status = 'superseded'
                WHERE id IN (SELECT t.id FROM backend_operation_tasks t
                    JOIN backend_operations o ON o.id = t.operation_id
                    WHERE o.profile_id = ? AND t.node_key = ? AND t.status IN ('awaiting_executor', 'blocked'))""",
                (row['profile_id'], row['node_key']))
            for item in affected:
                self.refresh_operation(conn, item['operation_id'])
            revision = conn.execute("""UPDATE backend_profiles SET desired_revision = desired_revision + 1
                WHERE id = ? RETURNING desired_revision""", (row['profile_id'],)).fetchone()
            if revision is None:
                raise AccessDenied('resource_not_found', 404)
            operation = OperationRepository.record(conn, actor, row['profile_id'],
                OperationRepository.targets(conn, row['profile_id']))
            conn.execute("""INSERT INTO backend_repairs(task_id, actor_id, operation_id, observation_json, created_at)
                VALUES (?, ?, ?, ?, ?)""", (task_id, actor.account.id, operation['id'],
                json.dumps(observation, sort_keys=True), datetime.now(timezone.utc).isoformat()))
        return {'operation_id': operation['id'], 'observation': observation}

    def reconcile_completed(self):
        """Recover confirmed successes using read-only driver/agent journal queries.

        Missing, running or failed journal entries do not establish node state.
        No mutation or fresh command identity is submitted by this method.
        """
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM backend_operation_tasks WHERE status = 'blocked' ORDER BY id").fetchall()
        recovered = 0
        for row in rows:
            try:
                result = self.driver.lookup(row['id'], json.loads(row['intent_json']))
                if not result['succeeded']:
                    continue
            except Exception:
                continue
            with self.db.transaction() as conn:
                changed = conn.execute("""UPDATE backend_operation_tasks
                    SET status = 'succeeded', driver_operation_id = ?, result_json = ?
                    WHERE id = ? AND status = 'blocked' RETURNING id""",
                    (result['driver_operation_id'], result.get('result_json'), row['id'])).fetchone()
                if changed is not None:
                    self.refresh_operation(conn, row['operation_id'])
                    recovered += 1
        return recovered

    def inspect_blocked(self):
        """Persist live observations, without declaring uncertain execution done."""
        with self.db.connect() as conn:
            rows = conn.execute("SELECT id, intent_json FROM backend_operation_tasks WHERE status = 'blocked'").fetchall()
        count = 0
        for row in rows:
            try:
                value = self.driver.inspect(json.loads(row['intent_json']))
                keys = {'disk_present', 'live_present', 'identity_matches', 'config_available'}
                if set(value) != keys or any(type(v) is not bool for v in value.values()):
                    raise ValueError('invalid inspection')
                observation = {'available': True, **value}
                count += 1
            except Exception:
                # Replace old evidence: an unavailable node must not look current.
                observation = {'available': False}
            with self.db.transaction() as conn:
                conn.execute("UPDATE backend_operation_tasks SET inspection_json = ?, inspected_at = ? WHERE id = ? AND status = 'blocked'",
                    (json.dumps(observation), datetime.now(timezone.utc).isoformat(), row['id']))
        return count

    def run_one(self):
        with self.db.transaction() as conn:
            # Preserve profile revision order on each node, including revocation.
            # Any uncertain result blocks later work, even for another profile.
            row = conn.execute("""SELECT t.*, o.profile_id, o.desired_revision
                FROM backend_operation_tasks t JOIN backend_operations o ON o.id = t.operation_id
                WHERE t.status = 'awaiting_executor'
                  AND NOT EXISTS (SELECT 1 FROM backend_node_settings_tasks ns
                      WHERE ns.node_key = t.node_key AND ns.status IN ('awaiting_executor', 'running', 'blocked'))
                  AND NOT EXISTS (SELECT 1 FROM backend_node_jobs nj
                      WHERE nj.node_key = t.node_key AND nj.status IN ('awaiting_executor', 'running', 'blocked'))
                  AND NOT EXISTS (SELECT 1 FROM backend_operation_tasks b
                      WHERE b.node_key = t.node_key AND b.status IN ('running', 'blocked'))
                  AND NOT EXISTS (SELECT 1 FROM backend_operation_tasks earlier
                      JOIN backend_operations eo ON eo.id = earlier.operation_id
                      WHERE earlier.node_key = t.node_key AND eo.profile_id = o.profile_id
                        AND eo.desired_revision < o.desired_revision
                        AND earlier.status NOT IN ('succeeded', 'superseded'))
                ORDER BY o.created_at, o.id, t.id LIMIT 1""").fetchone()
            if row is None:
                return False
            node = conn.execute('SELECT enabled FROM backend_nodes WHERE key = ?', (row['node_key'],)).fetchone()
            device = conn.execute('SELECT status FROM backend_devices WHERE id=?', (row['device_id'],)).fetchone() if row['device_id'] else None
            device_inactive = bool(row['device_id']) and (device is None or device['status'] != 'active')
            if row['action'] == 'ensure' and (node is None or not node['enabled'] or device_inactive):
                # A drain may leave earlier ensures queued. Retire them before
                # sending any remote mutation, then let the delete revision run.
                conn.execute("UPDATE backend_operation_tasks SET status = 'superseded' WHERE id = ?", (row['id'],))
                self.refresh_operation(conn, row['operation_id'])
                return True
            claimed = conn.execute("""UPDATE backend_operation_tasks SET status = 'running'
                WHERE id = ? AND status = 'awaiting_executor' RETURNING id""", (row['id'],)).fetchone()
            if claimed is None:
                return False
            conn.execute("UPDATE backend_operations SET status = 'running' WHERE id = ?", (row['operation_id'],))
        # Commit claim before remote work. Do not hold a DB transaction over RPC.
        try:
            intent = json.loads(row['intent_json'])
            expiry = intent.get('expires_at')
            if intent['action'] == 'ensure' and expiry is not None and datetime.fromisoformat(expiry) <= datetime.now(timezone.utc):
                # Never provision an expired snapshot. A fresh revocation intent
                # must be reconciled rather than changing this command's payload.
                result = {'succeeded': False}
            else:
                result = self.driver.execute(row['id'], intent)
            status = 'succeeded' if result['succeeded'] else 'blocked'
        except Exception:
            # No raw exception/credential data enters operation responses.
            result, status = {}, 'blocked'
        with self.db.transaction() as conn:
            conn.execute("""UPDATE backend_operation_tasks SET status = ?, driver_operation_id = ?, result_json = ?
                WHERE id = ? AND status = 'running'""", (status, result.get('driver_operation_id'), result.get('result_json'), row['id']))
            self.refresh_operation(conn, row['operation_id'])
        return True


def main():
    parser = argparse.ArgumentParser(description='Execute backend intents under a single-host exclusive lock')
    parser.add_argument('--lock-file', required=True, help='Shared, stable lock path for every worker invocation')
    parser.add_argument('--driver', default='127.0.0.1:50051')
    args = parser.parse_args()
    # Must be the same path on every invocation; distributed workers are not supported.
    fd = os.open(args.lock_file, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        from db import get_db
        from .driver_transport import GrpcIntentDriver, local_channel
        db = get_db()
        from db.migrations import check_schema
        check_schema(db)
        with local_channel(args.driver) as channel:
            executor = IntentExecutor(db, GrpcIntentDriver(channel))
            node_executor = NodeSettingsExecutor(executor.db, executor.driver)
            from .node_operations import NodeOperations
            node_jobs = NodeOperations(executor.db, executor.driver)
            from .node_removal import NodeRemovalService
            removals = NodeRemovalService(executor.db, executor.driver)
            config_executor = ConfigIssuanceService(executor.db, executor.driver)
            from .temporary_configs import TemporaryConfigService
            temporary_executor = TemporaryConfigService(executor.db, executor.driver)
            rollout_executor = AgentRolloutService(executor.db)
            from .updates import UpdateService
            update_executor = UpdateService(executor.db, executor.driver)
            update_executor.recover()
            from .backups import BackupService
            backup_executor = BackupService(executor.db)
            from .system_cleanup import SystemCleanupService
            system_cleanup = SystemCleanupService(executor.db, executor.driver)
            from .maintenance_gate import active
            with executor.db.connect() as conn:
                cleaning = bool(active(conn))
            try:
                if not cleaning:
                    backup_executor.scheduled()
            except Exception:
                import logging
                logging.getLogger(__name__).warning('Scheduled backup failed')
            try:
                if not cleaning:
                    update_executor.auto_check()
            except Exception:
                # An upstream check must not prevent provisioning or cleanup.
                import logging
                logging.getLogger(__name__).warning('Automatic update check failed')
            executor.recover()
            node_executor.recover()
            node_jobs.recover()
            node_jobs.reconcile_completed()
            config_executor.recover()
            temporary_executor.recover()
            temporary_executor.scheduled()
            temporary_executor.reconcile()
            rollout_executor.recover()
            executor.reconcile_completed()
            executor.queue_expired_profiles()
            from .devices import DeviceRepository
            with executor.db.transaction() as conn:
                DeviceRepository.settle_deletions(conn)
            node_executor.reconcile_completed()
            executor.inspect_blocked()
            while system_cleanup.run_one() or backup_executor.run_one() or update_executor.run_one() or rollout_executor.run_one() or removals.run_one() or node_jobs.run_one() or node_executor.run_one() or executor.run_one() or temporary_executor.run_one() or config_executor.run_one():
                pass
            from .alerts import AlertService
            try:
                AlertService(executor.db, GrpcIntentDriver(channel, timeout=5)).scheduled()
            except Exception:
                import logging
                logging.getLogger(__name__).warning('Alert monitor requires attention')
            from .traffic import TrafficService
            try:
                TrafficService(executor.db, GrpcIntentDriver(channel, timeout=10)).scheduled()
            except Exception:
                import logging
                logging.getLogger(__name__).warning('Traffic collection requires attention')


if __name__ == '__main__':
    main()
