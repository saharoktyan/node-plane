"""Version selection and durable update batches for the backend-owned registry."""
import json
import os
import subprocess
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from uuid import UUID, uuid4, uuid5

from .authorization import AccessDenied, require_permission, Actor, Account, Principal, PrincipalKind, ADMIN_PERMISSIONS
from .agent_rollout import AgentRolloutService
from .node_operations import NodeOperations


def same_commit(actual, desired):
    actual, desired = str(actual or ''), str(desired or '')
    return (len(actual) >= 7 and len(desired) >= 7 and actual != 'unknown'
            and desired != 'unknown' and (actual.startswith(desired) or desired.startswith(actual)))


class UpdateService:
    def __init__(self, db, driver=None, updater=None, runner=None):
        self.db, self.driver = db, driver
        self._updater = updater
        self.runner = runner

    def _verify_commit(self, read, field, expected):
        """Allow restarted services to become reachable; never replay installation."""
        failure = 'update_version_mismatch'
        for attempt in range(5):
            try:
                if same_commit(read().get(field), expected):
                    return True, None
                failure = 'update_version_mismatch'
            except Exception:
                failure = 'update_verification_unavailable'
            if attempt < 4:
                time.sleep(2)
        return False, failure

    @property
    def updater(self):
        if self._updater is None:
            from app.services import updates
            self._updater = updates
        return self._updater

    def initialize_schema(self):
        with self.db.transaction() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_controller_update_gate (
                id INTEGER PRIMARY KEY, job_id TEXT NOT NULL)''')
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_update_jobs (
                id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, command_key TEXT NOT NULL,
                kind TEXT NOT NULL, intent_json TEXT NOT NULL, status TEXT NOT NULL,
                result_json TEXT, created_at TEXT NOT NULL, UNIQUE(actor_id, command_key))''')
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_update_items (
                job_id TEXT NOT NULL, node_key TEXT NOT NULL, intent_json TEXT NOT NULL,
                status TEXT NOT NULL, child_id TEXT, error_code TEXT,
                PRIMARY KEY(job_id, node_key))''')

    def versions(self, actor, offset=0, limit=8):
        require_permission(actor, 'settings.manage')
        value = self.updater.list_available_versions()
        items = value.get('versions', [])
        return {'status': value.get('status'), 'branch': value.get('branch'),
                'items': items[offset:offset + limit], 'offset': offset,
                'next_offset': offset + limit if offset + limit < len(items) else None,
                'total': len(items)}

    def latest(self, actor):
        require_permission(actor, 'settings.manage')
        with self.db.connect() as conn:
            row = conn.execute('SELECT id FROM backend_update_jobs ORDER BY created_at DESC LIMIT 1').fetchone()
        return self.get(actor, row['id']) if row else None

    def rollout_overview(self, actor):
        require_permission(actor, 'settings.manage')
        from config import APP_COMMIT, APP_SEMVER
        with self.db.connect() as conn:
            nodes = conn.execute('''SELECT n.key, n.title, n.region, n.flag, n.desired_revision, c.transport, c.ssh_target
                FROM backend_nodes n LEFT JOIN backend_node_connections c ON c.node_key=n.key
                WHERE NOT EXISTS (SELECT 1 FROM backend_node_drains d WHERE d.node_key=n.key)
                ORDER BY n.region,n.title,n.key''').fetchall()
            latest = conn.execute("SELECT id FROM backend_update_jobs WHERE kind IN ('agents', 'runtimes') ORDER BY created_at DESC LIMIT 1").fetchone()
        try:
            binary = self.driver.binary_info()
            driver_status = 'current' if same_commit(binary.get('commit'), APP_COMMIT) else 'required'
        except Exception as exc:
            import grpc
            reachable_old = isinstance(exc, grpc.RpcError) and exc.code() in {
                grpc.StatusCode.UNIMPLEMENTED, grpc.StatusCode.FAILED_PRECONDITION}
            binary, driver_status = {}, 'required' if reachable_old else 'unknown'
        items = []
        for node in nodes:
            item = dict(node)
            try:
                facts = self.driver.inspect_node_services(node['key'])
                item.update(agent_status='current' if same_commit(facts.get('agent_commit'), APP_COMMIT) else 'required',
                    agent_commit=facts.get('agent_commit'), agent_version=facts.get('agent_version'),
                    runtime_status='current' if same_commit(facts.get('runtime_commit'), APP_COMMIT) else 'required',
                    runtime_commit=facts.get('runtime_commit'), runtime_version=facts.get('runtime_version'))
            except Exception:
                item.update(agent_status='unknown', runtime_status='unknown')
            items.append(item)
        active = self.get(actor, latest['id']) if latest else None
        return {'desired_version': APP_SEMVER, 'desired_commit': APP_COMMIT,
                'driver_status': driver_status, 'driver': binary, 'nodes': items,
                'agents_required': driver_status == 'required' or any(i['agent_status'] == 'required' for i in items),
                'runtimes_required': any(i['runtime_status'] == 'required' for i in items),
                'latest_job': active}

    def _previous(self, actor, key, kind, encoded):
        require_permission(actor, 'settings.manage')
        try:
            key = str(UUID(key))
        except (TypeError, ValueError, AttributeError):
            raise AccessDenied('invalid_idempotency_key', 422) from None
        with self.db.connect() as conn:
            row = conn.execute('SELECT * FROM backend_update_jobs WHERE actor_id=? AND command_key=?', (actor.account.id, key)).fetchone()
        if row and (row['kind'] != kind or row['intent_json'] != encoded):
            raise AccessDenied('idempotency_conflict', 409)
        return key, row

    def queue(self, actor, key, kind, *, target_ref=None, branch=None):
        if kind not in {'version', 'stack', 'agents', 'runtimes'}:
            raise AccessDenied('invalid_input', 422)
        if kind not in {'version', 'stack'} and (target_ref is not None or branch is not None):
            raise AccessDenied('invalid_input', 422)
        encoded = json.dumps({'target_ref': target_ref, 'branch': branch}, sort_keys=True)
        key, previous = self._previous(actor, key, kind, encoded)
        if previous:
            return self.get(actor, previous['id'])
        items = []
        plan = None
        if kind in {'version', 'stack'}:
            overview = self.updater.get_updates_overview()
            if not overview.get('update_supported'):
                raise AccessDenied('update_unsupported', 409)
            if branch != overview['branch']:
                raise AccessDenied('update_selection_changed', 409)
            catalog = self.updater.list_available_versions(branch=branch)
            selected = next((v for v in catalog.get('versions', []) if v['ref'] == target_ref), None)
            if catalog.get('status') == 'error' or selected is None or not (selected['allowed'] or kind == 'stack' and selected.get('action') == 'current'):
                raise AccessDenied('update_target_blocked', 409)
            resolved = selected.get('commit') if selected.get('kind') == 'head' else target_ref
            if selected.get('kind') == 'head':
                import re
                if not re.fullmatch(r'[0-9a-fA-F]{40}', resolved or ''):
                    raise AccessDenied('update_target_blocked', 409)
            plan = {'resolved_ref': resolved}
            if kind == 'stack':
                plan.update(expected_commit=selected.get('commit'), phase='core')
                if not selected.get('commit') or len(selected['commit']) != 40:
                    raise AccessDenied('update_target_blocked', 409)
                with self.db.connect() as conn:
                    nodes = conn.execute('''SELECT n.key,n.title,n.region,n.flag,n.desired_revision,n.applied_revision,
                        c.transport,c.ssh_target FROM backend_nodes n
                        JOIN backend_node_connections c ON c.node_key=n.key
                        WHERE NOT EXISTS (SELECT 1 FROM backend_node_drains d WHERE d.node_key=n.key)
                        ORDER BY n.region,n.title,n.key''').fetchall()
                items = [{**dict(n), 'expected_commit': selected['commit'], 'phase': 'agent'} for n in nodes]
        else:
            if not self.updater.is_driver_agents_setup_supported():
                raise AccessDenied('update_unsupported', 409)
            overview = self.rollout_overview(actor)
            items = [n for n in overview['nodes'] if n['agent_status' if kind == 'agents' else 'runtime_status'] == 'required']
            # Updating any agent also installs the controller driver. If only the
            # driver is outdated, use one configured node to run that installer.
            if kind == 'agents' and overview['driver_status'] == 'required':
                items.insert(0, {'key': '@driver'})
            if not items:
                raise AccessDenied('update_not_required', 409)
            for item in items:
                item['expected_commit'] = overview['desired_commit']
        job_id = str(uuid4())
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            account = conn.execute('UPDATE backend_accounts SET role=role WHERE id=? RETURNING role,status', (actor.account.id,)).fetchone()
            if not account or account['role'] != 'admin' or account['status'] != 'approved':
                raise AccessDenied('permission_denied')
            prior = conn.execute('SELECT * FROM backend_update_jobs WHERE actor_id=? AND command_key=?', (actor.account.id, key)).fetchone()
            if prior:
                if prior['kind'] != kind or prior['intent_json'] != encoded:
                    raise AccessDenied('idempotency_conflict', 409)
                return self.get(actor, prior['id'])
            if conn.execute("SELECT 1 FROM backend_update_jobs WHERE status IN ('awaiting_executor','running')").fetchone():
                raise AccessDenied('update_pending', 409)
            if kind in {'version', 'stack'}:
                for table in ('backend_agent_rollouts', 'backend_node_jobs', 'backend_node_settings_tasks',
                              'backend_operation_tasks', 'backend_config_issuances'):
                    if conn.execute(f"SELECT 1 FROM {table} WHERE status IN ('awaiting_executor','running')").fetchone():
                        raise AccessDenied('maintenance_busy', 409)
            conn.execute('INSERT INTO backend_update_jobs VALUES (?,?,?,?,?,?,?,?)',
                (job_id, actor.account.id, key, kind, encoded, 'awaiting_executor',
                 json.dumps(plan) if plan else None, datetime.now(timezone.utc).isoformat()))
            if kind == 'stack':
                conn.execute('INSERT INTO backend_controller_update_gate VALUES (1,?)', (job_id,))
            for item in items:
                conn.execute('INSERT INTO backend_update_items VALUES (?,?,?,?,?,?)',
                    (job_id, item['key'], json.dumps(item), 'awaiting_executor', None, None))
        return self.get(actor, job_id)

    def get(self, actor, job_id):
        require_permission(actor, 'settings.manage')
        with self.db.connect() as conn:
            row = conn.execute('SELECT * FROM backend_update_jobs WHERE id=?', (job_id,)).fetchone()
            if not row:
                raise AccessDenied('resource_not_found', 404)
            items = conn.execute('SELECT node_key,status,child_id,error_code,intent_json FROM backend_update_items WHERE job_id=? ORDER BY node_key', (job_id,)).fetchall()
        result = json.loads(row['result_json']) if row['result_json'] else None
        if row['kind'] == 'stack' and row['status'] == 'running':
            progress = self._stack_progress(job_id)
            if progress.get('components'):
                result = {**(result or {}), 'components': progress['components']}
        return {'id': row['id'], 'kind': row['kind'], 'status': row['status'],
                'created_at': row['created_at'], 'items': [{**{k: i[k] for k in ('node_key','status','child_id','error_code')},
                    **{k: json.loads(i['intent_json']).get(k) for k in ('title','region','flag','phase')}} for i in items],
                **json.loads(row['intent_json']),
                'result': result}

    def recover(self):
        # An interrupted systemd launch is uncertain; never launch it again.
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_update_jobs SET status='blocked' WHERE kind IN ('version','stack') AND status='running' AND result_json IS NULL")
            conn.execute("UPDATE backend_update_items SET status='blocked',error_code='execution_interrupted' WHERE node_key='@driver' AND status='running'")

    def cancel(self, actor, job_id):
        """Cancel only work that has not crossed any execution boundary.

        The caller holds the worker lock. Never clear an uncertain launch or
        installation merely because the operator requests cancellation.
        """
        require_permission(actor, 'maintenance.manage')
        with self.db.transaction() as conn:
            conn.execute('UPDATE backend_account_guard SET revision=revision+1 WHERE id=1')
            self._fresh_admin(conn, actor)
            job = conn.execute('SELECT * FROM backend_update_jobs WHERE id=?', (job_id,)).fetchone()
            if not job:
                raise AccessDenied('resource_not_found', 404)
            if job['status'] == 'cancelled':
                return self.get(actor, job_id)
            if job['status'] != 'awaiting_executor' or conn.execute("""SELECT 1 FROM backend_update_items
                    WHERE job_id=? AND (status!='awaiting_executor' OR child_id IS NOT NULL)""", (job_id,)).fetchone():
                raise AccessDenied('update_cancel_unsafe', 409)
            result = json.loads(job['result_json'] or '{}')
            result.update(cancelled_by=actor.account.id, cancelled_at=datetime.now(timezone.utc).isoformat())
            conn.execute("UPDATE backend_update_jobs SET status='cancelled',result_json=? WHERE id=?", (json.dumps(result), job_id))
            conn.execute("UPDATE backend_update_items SET status='skipped' WHERE job_id=?", (job_id,))
            conn.execute('DELETE FROM backend_controller_update_gate WHERE job_id=?', (job_id,))
        return self.get(actor, job_id)

    @staticmethod
    def _fresh_admin(conn, actor):
        account = conn.execute('SELECT role,status FROM backend_accounts WHERE id=?', (actor.account.id,)).fetchone()
        if not account or account['role'] != 'admin' or account['status'] != 'approved':
            raise AccessDenied('permission_denied')

    def recheck(self, actor, job_id):
        """Re-read an uncertain core outcome without repeating the installation.

        Failed rollback or missing evidence keeps the gate. Restored durable
        health/rollback evidence can release it after manual host recovery.
        """
        require_permission(actor, 'maintenance.manage')
        with self.db.transaction() as conn:
            conn.execute('UPDATE backend_account_guard SET revision=revision+1 WHERE id=1')
            self._fresh_admin(conn, actor)
            row = conn.execute('SELECT * FROM backend_update_jobs WHERE id=?', (job_id,)).fetchone()
            if not row:
                raise AccessDenied('resource_not_found', 404)
            if row['status'] != 'blocked' or row['kind'] != 'stack':
                raise AccessDenied('update_recovery_unavailable', 409)
            plan = json.loads(row['result_json'] or '{}')
            if not plan.get('unit_name') or plan.get('phase') != 'core':
                raise AccessDenied('update_recovery_unconfirmed', 409)
        self._run_stack_core(dict(row), actor)
        return self.get(actor, job_id)

    def run_one(self):
        with self.db.connect() as conn:
            jobs = conn.execute("SELECT * FROM backend_update_jobs WHERE status IN ('awaiting_executor','running') ORDER BY created_at").fetchall()
        for job in jobs:
            with self.db.connect() as conn:
                account = conn.execute('SELECT role,status FROM backend_accounts WHERE id=?', (job['actor_id'],)).fetchone()
            actor = Actor(Principal('updates-worker', PrincipalKind.SERVICE, ADMIN_PERMISSIONS),
                Account(job['actor_id'], account['role'], account['status'])) if account else None
            if actor is None or actor.account.role != 'admin' or actor.account.status != 'approved':
                self._finish(job['id'], 'blocked')
                return True
            if job['kind'] == 'stack':
                if self._run_stack_core(job, actor):
                    return True
                if json.loads(job['result_json'] or '{}').get('phase') != 'agents':
                    continue
            if job['kind'] == 'version':
                if job['status'] == 'awaiting_executor':
                    intent = json.loads(job['intent_json'])
                    plan = json.loads(job['result_json'])
                    with self.db.transaction() as conn:
                        conn.execute("UPDATE backend_update_jobs SET status='running', result_json=NULL WHERE id=?", (job['id'],))
                    try:
                        result = self.updater.schedule_update(branch=intent['branch'], target_ref=plan['resolved_ref'])
                    except Exception:
                        result = {'status': 'failed'}
                    self._finish(job['id'], 'running' if result.get('status') == 'running' else 'blocked',
                                 {'status': result.get('status'), 'unit_name': result.get('unit_name'), 'target_ref': plan['resolved_ref']})
                    return True
                state = self.updater.refresh_update_run_state()
                launch = json.loads(job['result_json'] or '{}')
                if state.get('last_run_unit') != launch.get('unit_name'):
                    self._finish(job['id'], 'blocked')
                    return True
                if state.get('last_run_status') in {'success', 'failed'}:
                    self._finish(job['id'], 'succeeded' if state['last_run_status'] == 'success' else 'blocked')
                    return True
                continue
            with self.db.connect() as conn:
                items = conn.execute('SELECT * FROM backend_update_items WHERE job_id=? ORDER BY node_key', (job['id'],)).fetchall()
            for item in items:
                if item['status'] == 'awaiting_executor':
                    intent = json.loads(item['intent_json'])
                    child_key = str(uuid5(UUID(job['id']), item['node_key'] + ':' + intent.get('phase', job['kind'])))
                    if item['node_key'] == '@driver':
                        with self.db.transaction() as conn:
                            conn.execute("UPDATE backend_update_items SET status='running' WHERE job_id=? AND node_key='@driver'", (job['id'],))
                            conn.execute("UPDATE backend_update_jobs SET status='running' WHERE id=?", (job['id'],))
                        try:
                            root = Path(os.environ['NODE_PLANE_APP_DIR'])
                            args = ['bash', str(root / 'scripts/setup_driver_agents.sh'), '--skip-agents', '--bin-source', 'auto']
                            succeeded = (self.runner(args) if self.runner else subprocess.run(args, cwd=root,
                                env={**os.environ, 'NODE_PLANE_INSTALL_RUST': 'no'}, capture_output=True,
                                timeout=1200, check=False).returncode == 0)
                            if succeeded:
                                succeeded, _ = self._verify_commit(self.driver.binary_info, 'commit', intent['expected_commit'])
                        except Exception:
                            succeeded = False
                        with self.db.transaction() as conn:
                            conn.execute("UPDATE backend_update_items SET status=? WHERE job_id=? AND node_key='@driver'",
                                ('succeeded' if succeeded else 'blocked', job['id']))
                        return True
                    try:
                        if job['kind'] == 'agents' or job['kind'] == 'stack' and intent.get('phase') == 'agent':
                            child = AgentRolloutService(self.db).request(actor, item['node_key'], child_key,
                                transport=intent['transport'], ssh_target=intent['ssh_target'], skip_driver=job['kind'] == 'stack')
                        else:
                            child = NodeOperations(self.db).queue(actor, item['node_key'], 'sync_runtime',
                                revision=intent['desired_revision'], command_key=child_key)
                        with self.db.transaction() as conn:
                            conn.execute("UPDATE backend_update_items SET status='running', child_id=? WHERE job_id=? AND node_key=?",
                                         (child['id'], job['id'], item['node_key']))
                            conn.execute("UPDATE backend_update_jobs SET status='running' WHERE id=?", (job['id'],))
                    except Exception as exc:
                        with self.db.transaction() as conn:
                            conn.execute("UPDATE backend_update_items SET status='blocked',error_code=? WHERE job_id=? AND node_key=?",
                                         (exc.code if isinstance(exc, AccessDenied) else 'update_queue_failed', job['id'], item['node_key']))
                    return True
                if item['status'] == 'running':
                    intent = json.loads(item['intent_json'])
                    agent_phase = job['kind'] == 'agents' or job['kind'] == 'stack' and intent.get('phase') == 'agent'
                    child = (AgentRolloutService(self.db).get(actor, item['child_id']) if agent_phase
                             else NodeOperations(self.db).get(actor, item['child_id']))
                    if child['status'] in {'succeeded', 'blocked', 'superseded'}:
                        status = child['status']
                        error_code = None if status == 'succeeded' else child.get('failure_code') or 'node_update_failed'
                        if status == 'succeeded':
                            field = 'agent_commit' if agent_phase else 'runtime_commit'
                            verified, error_code = self._verify_commit(
                                lambda: self.driver.inspect_node_services(item['node_key']), field,
                                json.loads(item['intent_json'])['expected_commit'])
                            if not verified:
                                status = 'blocked'
                        if job['kind'] == 'stack' and agent_phase and status == 'succeeded' and intent.get('applied_revision', 0):
                            intent['phase'] = 'runtime'
                            with self.db.transaction() as conn:
                                conn.execute("UPDATE backend_update_items SET status='awaiting_executor',child_id=NULL,intent_json=? WHERE job_id=? AND node_key=?",
                                    (json.dumps(intent), job['id'], item['node_key']))
                            return True
                        with self.db.transaction() as conn:
                            conn.execute('UPDATE backend_update_items SET status=?,error_code=? WHERE job_id=? AND node_key=?',
                                         (status, error_code, job['id'], item['node_key']))
                        return True
            states = {i['status'] for i in items}
            if not states & {'running', 'awaiting_executor'}:
                self._finish(job['id'], ('succeeded' if not states or states == {'succeeded'} else 'partial') if job['kind'] == 'stack' else ('succeeded' if states == {'succeeded'} else 'blocked'))
                return True
        return False

    def _stack_progress(self, job_id):
        try:
            path = Path(os.environ['NODE_PLANE_SHARED_DIR']) / 'data' / 'updates' / (str(UUID(job_id)) + '.json')
            value = json.loads(path.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, KeyError, ValueError):
            return {}

    def _run_stack_core(self, job, actor):
        plan = json.loads(job['result_json'] or '{}')
        if plan.get('phase') == 'agents':
            return False
        if job['status'] == 'awaiting_executor':
            # Record launch intent before crossing the systemd boundary. Recovery
            # never replays a launch whose outcome is uncertain.
            with self.db.transaction() as conn:
                conn.execute("UPDATE backend_update_jobs SET status='running',result_json=NULL WHERE id=?", (job['id'],))
            intent = json.loads(job['intent_json'])
            try:
                launched = self.updater.schedule_update(branch=intent['branch'],
                    target_ref=plan['resolved_ref'], stack_job_id=job['id'])
            except Exception:
                launched = {'status': 'failed'}
            plan.update(unit_name=launched.get('unit_name'))
            self._finish(job['id'], 'running' if launched.get('status') == 'running' else 'blocked', plan)
            return True
        progress = self._stack_progress(job['id'])
        state = self.updater.refresh_update_run_state()
        # systemctl/journal reads can outlast the final script steps. A progress
        # snapshot taken before those reads can still say "running" after the
        # unit has finished. Confirm terminal evidence after observing the unit,
        # including rollback markers; never reject a completed update using the
        # earlier snapshot and never replay the installation.
        if state.get('last_run_status') in {'success', 'failed'}:
            progress = self._stack_progress(job['id'])
        if progress.get('components'):
            plan['components'] = progress['components']
        if state.get('last_run_unit') != plan.get('unit_name'):
            self._finish(job['id'], 'blocked', {**plan, 'error_code': 'update_verification_unavailable'})
            return True
        if state.get('last_run_status') == 'failed':
            plan.update(rollback_status=progress.get('rollback_status', 'unknown'),
                error_code=progress.get('error_code', 'core_update_failed'))
            with self.db.transaction() as conn:
                conn.execute("UPDATE backend_update_items SET status='skipped' WHERE job_id=? AND status='awaiting_executor'", (job['id'],))
            self._finish(job['id'], 'rolled_back' if plan['rollback_status'] == 'succeeded' else 'blocked', plan)
            return True
        if state.get('last_run_status') == 'success':
            if progress.get('status') != 'succeeded':
                self._finish(job['id'], 'blocked', {**plan, 'error_code': 'update_verification_unavailable'})
            else:
                plan['phase'] = 'agents'
                plan.pop('error_code', None)
                plan.pop('rollback_status', None)
                self._finish(job['id'], 'running', plan)
            return True
        return False

    def _finish(self, job_id, status, result=None):
        with self.db.transaction() as conn:
            conn.execute('UPDATE backend_update_jobs SET status=?, result_json=COALESCE(?,result_json) WHERE id=?',
                         (status, json.dumps(result) if result is not None else None, job_id))
            if status in {'succeeded', 'partial', 'rolled_back', 'cancelled'} or result and result.get('phase') == 'agents':
                conn.execute('DELETE FROM backend_controller_update_gate WHERE job_id=?', (job_id,))

    def auto_check(self):
        from app.services import app_settings
        if not app_settings.is_updates_auto_check_enabled():
            return
        last = app_settings.get_update_state().get('last_checked_at')
        try:
            due = not last or datetime.fromisoformat(last.replace('Z', '+00:00')) < datetime.now(timezone.utc) - timedelta(hours=1)
        except ValueError:
            due = True
        if due:
            self.updater.check_for_updates()
