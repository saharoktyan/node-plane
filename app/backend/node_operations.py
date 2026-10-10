"""Durable backend node installation and maintenance jobs."""
import json
from types import SimpleNamespace
from uuid import uuid4

from .authorization import AccessDenied, require_permission
from .node_settings import _key, _snapshot, persist_selected_port
from .operations import OperationRepository
from .installation_progress import read_progress

ACTIONS = {'bootstrap', 'reinstall_keep', 'reinstall_clean', 'cleanup_runtime',
           'install_docker', 'check_ports', 'open_ports', 'sync_runtime',
           'sync_env', 'sync_xray', 'regenerate_entropy', 'reconcile_access'}
REBUILD = {'bootstrap', 'reinstall_keep', 'reinstall_clean', 'sync_env', 'sync_xray'}
INVALIDATE = REBUILD | {'cleanup_runtime', 'regenerate_entropy'}


class NodeOperations:
    def __init__(self, db, driver=None):
        self.db, self.driver = db, driver

    def initialize_schema(self):
        with self.db.transaction() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_node_jobs (
                id TEXT PRIMARY KEY, node_key TEXT NOT NULL, actor_id TEXT NOT NULL,
                command_key TEXT NOT NULL, action TEXT NOT NULL, revision INTEGER NOT NULL,
                intent_json TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT,
                UNIQUE(actor_id, command_key)
            )''')

    @staticmethod
    def public(row):
        return {key: row[key] for key in ('id', 'node_key', 'action', 'revision', 'status')} | {
            'result': json.loads(row['result_json']) if row['result_json'] else None,
            'progress': read_progress(row)}

    def get(self, actor, job_id):
        require_permission(actor, 'nodes.manage')
        with self.db.connect() as conn:
            row = conn.execute('SELECT * FROM backend_node_jobs WHERE id = ?', (job_id,)).fetchone()
        if row is None:
            raise AccessDenied('resource_not_found', 404)
        return self.public(row)

    def queue(self, actor, node_key, action, *, revision, command_key, bootstrap_id=None):
        require_permission(actor, 'nodes.manage')
        key = _key(command_key)
        if action not in ACTIONS or type(revision) is not int or revision < 1:
            raise AccessDenied('invalid_input', 422)
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            account = conn.execute('UPDATE backend_accounts SET role = role WHERE id = ? RETURNING role, status',
                                   (actor.account.id,)).fetchone()
            if account is None or account['role'] != 'admin' or account['status'] != 'approved':
                raise AccessDenied('permission_denied')
            previous = conn.execute('SELECT * FROM backend_node_jobs WHERE actor_id = ? AND command_key = ?',
                                    (actor.account.id, key)).fetchone()
            if previous:
                if previous['node_key'] != node_key or previous['action'] != action or json.loads(previous['intent_json'])['requested_revision'] != revision:
                    raise AccessDenied('idempotency_conflict', 409)
                return self.public(previous)
            node = conn.execute('UPDATE backend_nodes SET enabled = enabled WHERE key = ? RETURNING *', (node_key,)).fetchone()
            if node is None:
                raise AccessDenied('resource_not_found', 404)
            from .node_bootstrap import require_idle
            require_idle(conn, node_key, bootstrap_id)
            if node['desired_revision'] != revision:
                raise AccessDenied('revision_conflict', 412)
            if conn.execute('SELECT 1 FROM backend_node_drains WHERE node_key = ?', (node_key,)).fetchone():
                raise AccessDenied('node_already_draining', 409)
            for table in ('backend_node_jobs', 'backend_node_settings_tasks', 'backend_operation_tasks'):
                if conn.execute(f"SELECT 1 FROM {table} WHERE node_key = ? AND status IN ('awaiting_executor', 'running', 'blocked')", (node_key,)).fetchone():
                    raise AccessDenied('node_operation_pending', 409)
            if action in {'sync_xray'} and 'xray' not in json.loads(node['protocols_json']):
                raise AccessDenied('config_not_supported', 422)
            if action == 'regenerate_entropy' and 'awg' not in json.loads(node['protocols_json']):
                raise AccessDenied('config_not_supported', 422)
            snapshot = ({'node_key': node_key, 'revision': node['desired_revision'],
                         'protocols': json.loads(node['protocols_json']), 'settings': json.loads(node['settings_json'])}
                        if action in {'install_docker', 'sync_runtime', 'cleanup_runtime'} else _snapshot(node))
            job_id = str(uuid4())
            if action in INVALIDATE:
                snapshot['revision'] += 1
                conn.execute('UPDATE backend_nodes SET enabled = 0, desired_revision = ? WHERE key = ?',
                             (snapshot['revision'], node_key))
            snapshot.update(command_id=job_id, requested_revision=revision)
            conn.execute('''INSERT INTO backend_node_jobs
                (id, node_key, actor_id, command_key, action, revision, intent_json, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'awaiting_executor')''',
                (job_id, node_key, actor.account.id, key, action, snapshot['revision'], json.dumps(snapshot)))
            return self.public(conn.execute('SELECT * FROM backend_node_jobs WHERE id = ?', (job_id,)).fetchone())

    def recover(self):
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_node_jobs SET status = 'blocked' WHERE status = 'running'")

    def resolve(self, actor, job_id):
        require_permission(actor, 'maintenance.manage')
        with self.db.connect() as conn:
            row = conn.execute('SELECT * FROM backend_node_jobs WHERE id = ?', (job_id,)).fetchone()
        if row is None:
            raise AccessDenied('resource_not_found', 404)
        if row['status'] != 'blocked':
            raise AccessDenied('task_not_blocked', 409)
        # Explicit resolution may restore missing runtime helpers before reading
        # or retiring the command. The resolve_* helper never runs its mutation.
        # Passive journal polling below must remain recover-only.
        result = self.driver.node_action(job_id, 'resolve_' + row['action'], json.loads(row['intent_json']))
        if result.get('retired') is not True:
            self._finish(row, result)
        else:
            if (result.get('node_key'), result.get('action'), result.get('revision')) != (row['node_key'], row['action'], row['revision']):
                raise ValueError('invalid node operation retirement')
            with self.db.transaction() as conn:
                conn.execute("UPDATE backend_node_jobs SET status = 'superseded' WHERE id = ? AND status = 'blocked'", (job_id,))
        return self.get(actor, job_id)

    def _finish(self, row, result):
        if (not isinstance(result, dict) or result.get('node_key') != row['node_key']
                or result.get('action') != row['action'] or result.get('revision') != row['revision']):
            raise ValueError('invalid node operation result')
        with self.db.transaction() as conn:
            conn.execute('UPDATE backend_account_guard SET revision=revision+1 WHERE id=1')
            current = conn.execute('SELECT status FROM backend_node_jobs WHERE id = ?', (row['id'],)).fetchone()
            if current is None or current['status'] not in {'running', 'blocked'}:
                return
            if 'awg_port' in result['result']:
                persist_selected_port(conn, row['node_key'], row['revision'],
                    json.loads(row['intent_json']), result['result'])
            conn.execute("UPDATE backend_node_jobs SET status = 'succeeded', result_json = ? WHERE id = ?",
                         (json.dumps(result['result']), row['id']))
            if row['action'] in INVALIDATE and row['action'] != 'cleanup_runtime':
                conn.execute('UPDATE backend_nodes SET applied_revision = ?, enabled = 1 WHERE key = ? AND desired_revision = ?',
                             (row['revision'], row['node_key'], row['revision']))
            if row['action'] in INVALIDATE | {'reconcile_access'} and row['action'] != 'cleanup_runtime':
                actor = SimpleNamespace(account=SimpleNamespace(id=row['actor_id']))
                from .grant_policies import reconcile
                changed = reconcile(conn, actor)
                # Increment profile revisions so the agent cannot return an old
                # completed ensure from its journal after clean reinstall.
                profiles = conn.execute('''SELECT DISTINCT p.id FROM backend_profiles p
                    JOIN backend_grants g ON g.profile_id = p.id WHERE g.node_key = ?''', (row['node_key'],)).fetchall()
                for profile in profiles:
                    if profile['id'] in changed:
                        continue
                    previous = OperationRepository.targets(conn, profile['id'])
                    conn.execute('UPDATE backend_profiles SET desired_revision = desired_revision + 1 WHERE id = ?', (profile['id'],))
                    OperationRepository.record(conn, actor, profile['id'], previous)
            if row['action'] in INVALIDATE:
                conn.execute("UPDATE backend_config_issuances SET status = 'superseded', result_json = NULL WHERE node_key = ? AND status = 'succeeded'",
                             (row['node_key'],))

    def reconcile_completed(self):
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM backend_node_jobs WHERE status = 'blocked'").fetchall()
        for row in rows:
            try:
                self._finish(row, self.driver.node_action(row['id'], row['action'], json.loads(row['intent_json']), recover=True))
            except Exception:
                continue

    def run_one(self):
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM backend_node_jobs WHERE status = 'awaiting_executor' ORDER BY id LIMIT 1").fetchone()
            if row is None:
                return False
            conn.execute("UPDATE backend_node_jobs SET status = 'running' WHERE id = ?", (row['id'],))
        try:
            intent = json.loads(row['intent_json'])
            if row['action'] == 'reconcile_access':
                result = {'node_key': row['node_key'], 'action': row['action'], 'revision': row['revision'], 'result': {'reconciled': True}}
            else:
                from .installation_progress import observe_node_job
                with observe_node_job(row, self.driver):
                    result = self.driver.node_action(row['id'], row['action'], intent)
            self._finish(row, result)
        except Exception as error:
            from .recovery import failure_code
            with self.db.transaction() as conn:
                conn.execute("UPDATE backend_node_jobs SET status = 'blocked',result_json=? WHERE id = ? AND status = 'running'", (json.dumps({'error_code': failure_code(error)}), row['id']))
        return True
