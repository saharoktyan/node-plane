"""Version selection and durable update batches for the backend-owned registry."""
import json
import os
import subprocess
import time
import hashlib
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
    def __init__(self, db, driver=None, updater=None, runner=None, observation_cache=None):
        self.db, self.driver = db, driver
        self._updater = updater
        self.runner = runner
        self.observation_cache = observation_cache if observation_cache is not None else {}

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
            conn.execute('CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
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
        value = self.updater.list_available_versions(use_cache=True)
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

    def dismissed_results(self, actor):
        require_permission(actor, 'settings.manage')
        with self.db.connect() as conn:
            row = conn.execute('SELECT value FROM schema_meta WHERE key=?',
                ('updates.dismissed.' + actor.account.id,)).fetchone()
        return json.loads(row['value']) if row else []

    def dismiss_result(self, actor, identity):
        require_permission(actor, 'settings.manage')
        job = self.get(actor, identity)
        if job['status'] in {'awaiting_executor', 'running'}:
            raise AccessDenied('update_result_active', 409)
        key = 'updates.dismissed.' + actor.account.id
        with self.db.transaction() as conn:
            row = conn.execute('SELECT value FROM schema_meta WHERE key=?', (key,)).fetchone()
            dismissed = json.loads(row['value']) if row else []
            if identity not in dismissed:
                dismissed.append(identity)
            conn.execute('INSERT INTO schema_meta(key,value) VALUES (?,?) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                (key, json.dumps(dismissed)))
        return {'dismissed_job_id': identity}

    def rollout_overview(self, actor, *, use_cache=False):
        require_permission(actor, 'settings.manage')
        from config import APP_COMMIT, APP_SEMVER
        with self.db.connect() as conn:
            nodes = conn.execute('''SELECT n.key, n.title, n.region, n.flag, n.desired_revision, c.transport, c.ssh_target
                FROM backend_nodes n LEFT JOIN backend_node_connections c ON c.node_key=n.key
                WHERE NOT EXISTS (SELECT 1 FROM backend_node_drains d WHERE d.node_key=n.key)
                ORDER BY n.region,n.title,n.key''').fetchall()
            latest = conn.execute("SELECT id FROM backend_update_jobs WHERE kind IN ('agents', 'runtimes') ORDER BY created_at DESC LIMIT 1").fetchone()
        signature = (APP_COMMIT, tuple(tuple(dict(n).items()) for n in nodes))
        cached = self.observation_cache.get('rollout') if use_cache else None
        cache_valid = cached and cached['signature'] == signature and time.monotonic() - cached['time'] < 3
        if cache_valid:
            # Permissions, topology, and job state are always read afresh.
            with self.db.connect() as conn:
                pending = any(conn.execute(f"SELECT 1 FROM {table} WHERE status IN ('awaiting_executor','running') LIMIT 1").fetchone()
                    for table in ('backend_update_jobs', 'backend_agent_rollouts', 'backend_node_jobs', 'backend_node_bootstraps'))
            cache_valid = not pending
        if cache_valid:
            binary, driver_status, items = cached['binary'], cached['driver_status'], cached['items']
        else:
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
            if use_cache:
                self.observation_cache['rollout'] = {'signature': signature, 'time': time.monotonic(),
                    'binary': binary, 'driver_status': driver_status, 'items': items}
        active = self.get(actor, latest['id']) if latest else None
        return {'desired_version': APP_SEMVER, 'desired_commit': APP_COMMIT,
                'driver_status': driver_status, 'driver': binary, 'nodes': items,
                'agents_required': driver_status == 'required' or any(i['agent_status'] == 'required' for i in items),
                'runtimes_required': any(i['runtime_status'] == 'required' for i in items),
                'latest_job': active, 'dismissed_job_ids': self.dismissed_results(actor)}

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
            plan = {'resolved_ref': resolved, 'expected_commit': selected.get('commit'),
                    'launch_state': 'not_started', 'phase': 'core'}
            if kind == 'stack':
                plan.update(phase='core')
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
            if conn.execute("SELECT 1 FROM backend_node_bootstraps WHERE status IN ('awaiting_executor','running','blocked')").fetchone():
                raise AccessDenied('maintenance_busy', 409)
            if kind in {'version', 'stack'}:
                for table in ('backend_node_bootstraps', 'backend_agent_rollouts', 'backend_node_jobs', 'backend_node_settings_tasks',
                              'backend_operation_tasks', 'backend_config_issuances'):
                    if conn.execute(f"SELECT 1 FROM {table} WHERE status IN ('awaiting_executor','running')").fetchone():
                        raise AccessDenied('maintenance_busy', 409)
            conn.execute('INSERT INTO backend_update_jobs VALUES (?,?,?,?,?,?,?,?)',
                (job_id, actor.account.id, key, kind, encoded, 'awaiting_executor',
                 json.dumps(plan) if plan else None, datetime.now(timezone.utc).isoformat()))
            if kind in {'version', 'stack'}:
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
        if row['kind'] in {'stack', 'version'} and row['status'] == 'running':
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
            for row in conn.execute("SELECT id,result_json FROM backend_update_jobs WHERE kind IN ('version','stack') AND status='running'").fetchall():
                if json.loads(row['result_json'] or '{}').get('launch_state') == 'dispatching':
                    conn.execute("UPDATE backend_update_jobs SET status='blocked' WHERE id=?", (row['id'],))
            conn.execute("UPDATE backend_update_items SET status='blocked',error_code='driver_install_unconfirmed' WHERE node_key='@driver' AND status='running'")

    @staticmethod
    def _child_key(job, item):
        intent = json.loads(item['intent_json'])
        phase = intent.get('phase', job['kind'])
        suffix = phase + ':' + intent['retired_child_id'] if intent.get('retired_child_id') else item['node_key'] + ':' + phase
        return str(uuid5(UUID(job['id']), suffix))

    def _recovery_job(self, conn, actor, job_id):
        conn.execute('UPDATE backend_account_guard SET revision=revision+1 WHERE id=1')
        self._fresh_admin(conn, actor)
        job = conn.execute('SELECT * FROM backend_update_jobs WHERE id=?', (job_id,)).fetchone()
        if not job:
            raise AccessDenied('resource_not_found', 404)
        gate = conn.execute('SELECT job_id FROM backend_controller_update_gate WHERE id=1').fetchone()
        if gate and gate['job_id'] != job_id:
            raise AccessDenied('update_pending', 409)
        if conn.execute("SELECT 1 FROM backend_system_cleanup_jobs WHERE status IN ('queued','running','awaiting_shutdown','blocked') LIMIT 1").fetchone():
            raise AccessDenied('system_cleanup_in_progress', 409)
        if conn.execute("SELECT 1 FROM backend_backup_jobs WHERE action='restore' AND status IN ('awaiting_executor','running') LIMIT 1").fetchone():
            raise AccessDenied('restore_in_progress', 409)
        return dict(job)

    def _bound_child(self, conn, job, item):
        intent = json.loads(item['intent_json'])
        agent = job['kind'] == 'agents' or job['kind'] == 'stack' and intent.get('phase') == 'agent'
        table = 'backend_agent_rollouts' if agent else 'backend_node_jobs'
        owner = 'actor_account_id' if agent else 'actor_id'
        key = self._child_key(job, item)
        if item['child_id']:
            child = conn.execute(f'SELECT * FROM {table} WHERE id=?', (item['child_id'],)).fetchone()
            if child is None:
                raise AccessDenied('update_child_missing', 409)
        else:
            child = conn.execute(f'SELECT * FROM {table} WHERE {owner}=? AND command_key=?', (job['actor_id'], key)).fetchone()
        if child is None:
            return None
        if (child['node_key'], child[owner], child['command_key']) != (item['node_key'], job['actor_id'], key):
            raise AccessDenied('update_child_mismatch', 409)
        if agent:
            saved = json.loads(child['intent_json'])
            connection = conn.execute('SELECT transport,ssh_target FROM backend_node_connections WHERE node_key=?', (item['node_key'],)).fetchone()
            expected = (intent.get('transport'), intent.get('ssh_target'))
            if ((saved.get('transport'), saved.get('ssh_target')) != expected
                    or saved.get('ssh_port', 22) != intent.get('ssh_port', 22)
                    or bool(saved.get('skip_driver')) != (job['kind'] == 'stack')
                    or saved.get('install_rust', False)
                    or not connection or (connection['transport'], connection['ssh_target']) != expected):
                raise AccessDenied('update_child_mismatch', 409)
        elif child['action'] != 'sync_runtime' or child['revision'] != intent.get('desired_revision'):
            raise AccessDenied('update_child_mismatch', 409)
        return dict(child)

    def cancel(self, actor, job_id):
        """Stop only proven unstarted or terminal work; never call RPC in a transaction."""
        require_permission(actor, 'maintenance.manage')
        with self.db.transaction() as conn:
            job = self._recovery_job(conn, actor, job_id)
        if job['status'] == 'cancelled':
            return self.get(actor, job_id)
        plan = json.loads(job['result_json'] or '{}')
        terminal_version = False
        if (job['status'] == 'blocked' and job['kind'] == 'version' and plan.get('phase') != 'core'
                and plan.get('launch_state') != 'not_started'):
            state = self.updater.refresh_update_run_state()
            terminal_version = bool(plan.get('unit_name') and state.get('last_run_unit') == plan['unit_name']
                and not state.get('last_run_unit_missing') and not state.get('last_run_observation_error')
                and state.get('last_run_status') in {'success', 'failed'})
            if not terminal_version:
                raise AccessDenied('update_cancel_unsafe', 409)
        with self.db.transaction() as conn:
            current = self._recovery_job(conn, actor, job_id)
            if current['status'] != job['status'] or current['result_json'] != job['result_json']:
                raise AccessDenied('update_selection_changed', 409)
            if job['status'] not in {'awaiting_executor', 'blocked'}:
                raise AccessDenied('update_cancel_unsafe', 409)
            items = conn.execute('SELECT * FROM backend_update_items WHERE job_id=? ORDER BY node_key', (job_id,)).fetchall()
            if job['status'] == 'awaiting_executor':
                if any(i['status'] != 'awaiting_executor' or i['child_id'] for i in items):
                    raise AccessDenied('update_cancel_unsafe', 409)
            elif (job['kind'] == 'stack' and plan.get('phase') != 'agents'
                    or job['kind'] == 'version' and plan.get('phase') == 'core'):
                if plan.get('launch_state') != 'not_started' or any(i['status'] != 'awaiting_executor' or i['child_id'] for i in items):
                    raise AccessDenied('update_cancel_unsafe', 409)
            elif job['kind'] == 'version' and not terminal_version and plan.get('launch_state') != 'not_started':
                raise AccessDenied('update_cancel_unsafe', 409)
            for item in items:
                if item['node_key'] == '@driver':
                    if item['status'] not in {'awaiting_executor', 'succeeded', 'skipped', 'cancelled'} and not (item['status'] == 'blocked' and item['error_code'] == 'driver_install_failed'):
                        raise AccessDenied('update_cancel_unsafe', 409)
                    continue
                require_permission(actor, 'nodes.manage')
                child = self._bound_child(conn, job, item)
                if child is None:
                    continue  # No durable child exists, so its executor cannot have run.
                intent = json.loads(item['intent_json'])
                agent = job['kind'] == 'agents' or job['kind'] == 'stack' and intent.get('phase') == 'agent'
                if child['status'] in {'succeeded', 'superseded', 'cancelled'}:
                    continue
                safe = child['status'] == 'awaiting_executor' or agent and AgentRolloutService(self.db).preflight_failed(child)
                if not safe:
                    raise AccessDenied('update_cancel_unsafe', 409)
                table = 'backend_agent_rollouts' if agent else 'backend_node_jobs'
                conn.execute(f'UPDATE {table} SET status=? WHERE id=? AND status=?',
                    ('cancelled' if agent else 'superseded', child['id'], child['status']))
            plan.update(cancelled_by=actor.account.id, cancelled_at=datetime.now(timezone.utc).isoformat())
            conn.execute("UPDATE backend_update_jobs SET status='cancelled',result_json=? WHERE id=?", (json.dumps(plan), job_id))
            conn.execute("UPDATE backend_update_items SET status='skipped' WHERE job_id=? AND status!='succeeded'", (job_id,))
            conn.execute('DELETE FROM backend_controller_update_gate WHERE job_id=?', (job_id,))
        return self.get(actor, job_id)

    @staticmethod
    def _fresh_admin(conn, actor):
        account = conn.execute('SELECT role,status FROM backend_accounts WHERE id=?', (actor.account.id,)).fetchone()
        if not account or account['role'] != 'admin' or account['status'] != 'approved':
            raise AccessDenied('permission_denied')

    def recheck(self, actor, job_id):
        """Re-read an uncertain core outcome or children without repeating installation."""
        require_permission(actor, 'maintenance.manage')
        with self.db.transaction() as conn:
            job = self._recovery_job(conn, actor, job_id)
            if job['status'] != 'blocked':
                raise AccessDenied('task_not_blocked', 409)
        plan = json.loads(job['result_json'] or '{}')
        intent = json.loads(job['intent_json'] or '{}')

        if job['kind'] in {'stack', 'version'} and plan.get('phase') == 'core':
            if not plan.get('unit_name'):
                raise AccessDenied('update_recovery_unconfirmed', 409)
            progress = self._stack_progress(job['id'])
            state = self.updater.refresh_update_run_state()
            if not progress and state.get('last_run_unit') != plan.get('unit_name'):
                raise AccessDenied('update_recovery_unconfirmed', 409)
            self._run_stack_core(job, actor)
            return self.get(actor, job_id)

        if job['kind'] == 'version':
            unit_name = plan.get('unit_name')
            if not unit_name:
                raise AccessDenied('update_recovery_unconfirmed', 409)
            state = self.updater.refresh_update_run_state()
            if (state.get('last_run_unit') != unit_name or state.get('last_run_unit_missing')
                    or state.get('last_run_observation_error')):
                raise AccessDenied('update_recovery_unconfirmed', 409)
            if state.get('last_run_status') == 'success':
                target_commit = plan.get('expected_commit') or plan.get('resolved_ref') or intent.get('target_ref')
                verified = False
                plan['observed_unit_terminal'] = 'success'
                if target_commit and len(target_commit) == 40 and hasattr(self.driver, 'binary_info'):
                    verified, _ = self._verify_commit(self.driver.binary_info, 'commit', target_commit)
                if verified:
                    self._finish(job_id, 'succeeded', plan)
                else:
                    self._finish(job_id, 'blocked', {**plan, 'error_code': 'build_verification_failed'})
            elif state.get('last_run_status') == 'failed':
                self._finish(job_id, 'blocked', {**plan, 'observed_unit_terminal': 'failed', 'error_code': 'update_failed'})
            return self.get(actor, job_id)

        # Kind is 'agents', 'runtimes', or 'stack' in agents phase
        with self.db.connect() as conn:
            items = conn.execute('SELECT * FROM backend_update_items WHERE job_id=? ORDER BY node_key', (job_id,)).fetchall()

        for item in items:
            item_intent = json.loads(item['intent_json'])
            agent_phase = (job['kind'] == 'agents' or (job['kind'] == 'stack' and item_intent.get('phase') == 'agent'))

            if item['node_key'] == '@driver':
                if item['status'] == 'blocked':
                    verified, _ = self._verify_commit(self.driver.binary_info, 'commit', item_intent['expected_commit'])
                    if verified:
                        with self.db.transaction() as conn:
                            conn.execute("UPDATE backend_update_items SET status='succeeded',error_code=NULL WHERE job_id=? AND node_key='@driver'", (job_id,))
                continue

            require_permission(actor, 'nodes.manage')
            with self.db.transaction() as conn:
                self._recovery_job(conn, actor, job_id)
                child = self._bound_child(conn, job, item)
                if child and not item['child_id']:
                    conn.execute('UPDATE backend_update_items SET child_id=? WHERE job_id=? AND node_key=?', (child['id'], job_id, item['node_key']))
            if child:
                child_status = child['status']
                if child_status == 'succeeded':
                    field = 'agent_commit' if agent_phase else 'runtime_commit'
                    verified, error_code = self._verify_commit(
                        lambda: self.driver.inspect_node_services(item['node_key']), field,
                        item_intent['expected_commit'])
                    if verified:
                        if job['kind'] == 'stack' and agent_phase and item_intent.get('applied_revision', 0):
                            item_intent['phase'] = 'runtime'
                            with self.db.transaction() as conn:
                                conn.execute("UPDATE backend_update_items SET status='awaiting_executor',child_id=NULL,intent_json=?,error_code=NULL WHERE job_id=? AND node_key=?",
                                    (json.dumps(item_intent), job_id, item['node_key']))
                        else:
                            with self.db.transaction() as conn:
                                conn.execute("UPDATE backend_update_items SET status='succeeded',error_code=NULL WHERE job_id=? AND node_key=?",
                                    (job_id, item['node_key']))
                    else:
                        with self.db.transaction() as conn:
                            conn.execute("UPDATE backend_update_items SET status='blocked',error_code=? WHERE job_id=? AND node_key=?",
                                (error_code or 'build_verification_failed', job_id, item['node_key']))
                elif child_status in {'cancelled', 'superseded'}:
                    with self.db.transaction() as conn:
                        conn.execute("UPDATE backend_update_items SET status='cancelled',error_code=? WHERE job_id=? AND node_key=?",
                            (child.get('error_code') or 'child_cancelled', job_id, item['node_key']))
                elif child_status == 'blocked':
                    error_code = child.get('failure_code') or child.get('error_code') or 'node_update_failed'
                    with self.db.transaction() as conn:
                        conn.execute("UPDATE backend_update_items SET status='blocked',error_code=? WHERE job_id=? AND node_key=?",
                            (error_code, job_id, item['node_key']))

        with self.db.connect() as conn:
            updated_items = conn.execute("SELECT * FROM backend_update_items WHERE job_id=?", (job_id,)).fetchall()
        states = {i['status'] for i in updated_items}
        if not states & {'running', 'awaiting_executor'}:
            if not states or states == {'succeeded'}:
                self._finish(job_id, 'succeeded')
            elif states <= {'cancelled', 'skipped'}:
                self._finish(job_id, 'cancelled')
            elif job['kind'] == 'stack' and 'blocked' not in states:
                self._finish(job_id, 'partial')
            else:
                if 'blocked' in states:
                    self._finish(job_id, 'blocked')
                else:
                    self._finish(job_id, 'partial' if 'succeeded' in states else 'cancelled')
        return self.get(actor, job_id)

    def resume(self, actor, job_id):
        """Continue saved work only after its original execution boundary is settled.

        Caller holds the worker lock. Remote journal resolution is a separate
        child action; no network request or nested writer runs under this guard.
        """
        require_permission(actor, 'maintenance.manage')
        preparation_retries = []
        changes = []
        with self.db.transaction() as conn:
            job = self._recovery_job(conn, actor, job_id)
            plan = json.loads(job['result_json'] or '{}')
            if job['status'] != 'blocked':
                raise AccessDenied('task_not_blocked', 409)
            if job['kind'] not in {'agents', 'runtimes'} and not (job['kind'] == 'stack' and plan.get('phase') == 'agents'):
                raise AccessDenied('update_recovery_unavailable', 409)
            owner = conn.execute('SELECT role,status FROM backend_accounts WHERE id=?', (job['actor_id'],)).fetchone()
            if not owner or owner['role'] != 'admin' or owner['status'] != 'approved':
                raise AccessDenied('update_owner_unavailable', 409)
            from config import APP_COMMIT
            items = conn.execute('SELECT * FROM backend_update_items WHERE job_id=? ORDER BY node_key', (job_id,)).fetchall()
            for item in items:
                intent = json.loads(item['intent_json'])
                if not same_commit(intent.get('expected_commit'), APP_COMMIT):
                    raise AccessDenied('update_target_drifted', 409)
                if item['status'] in {'succeeded', 'cancelled', 'skipped'}:
                    continue
                if item['node_key'] == '@driver':
                    if item['status'] != 'awaiting_executor' and item['error_code'] != 'driver_install_failed':
                        raise AccessDenied('update_recovery_unconfirmed', 409)
                    changes.append((item['node_key'], 'awaiting_executor', None, intent))
                    continue
                require_permission(actor, 'nodes.manage')
                child = self._bound_child(conn, job, item)
                if child is None:
                    changes.append((item['node_key'], 'awaiting_executor', None, intent))
                    continue
                agent = job['kind'] == 'agents' or job['kind'] == 'stack' and intent.get('phase') == 'agent'
                if child['status'] in {'superseded', 'cancelled'}:
                    intent['retired_child_id'] = child['id']
                    node = conn.execute('SELECT desired_revision FROM backend_nodes WHERE key=?', (item['node_key'],)).fetchone()
                    if not node:
                        raise AccessDenied('resource_not_found', 404)
                    intent['desired_revision'] = node['desired_revision']
                    changes.append((item['node_key'], 'awaiting_executor', None, intent))
                elif agent and AgentRolloutService(self.db).preflight_failed(child):
                    preparation_retries.append(child['id'])
                    changes.append((item['node_key'], 'running', child['id'], intent))
                elif child['status'] in {'succeeded', 'awaiting_executor', 'running'}:
                    changes.append((item['node_key'], 'running', child['id'], intent))
                else:
                    raise AccessDenied('update_child_recovery_required', 409)
            if not changes:
                raise AccessDenied('update_recovery_unavailable', 409)
        # This helper performs its own admission transaction. Calling it while
        # holding backend_account_guard would deadlock PostgreSQL connections.
        for identity in preparation_retries:
            AgentRolloutService(self.db).resume_preflight(actor, identity)
        with self.db.transaction() as conn:
            current = self._recovery_job(conn, actor, job_id)
            if current['status'] != 'blocked' or current['result_json'] != job['result_json']:
                raise AccessDenied('update_selection_changed', 409)
            for node_key, status, child_id, intent in changes:
                conn.execute('UPDATE backend_update_items SET status=?,child_id=?,intent_json=?,error_code=NULL WHERE job_id=? AND node_key=?',
                    (status, child_id, json.dumps(intent), job_id, node_key))
            plan.pop('error_code', None)
            plan.update(resumed_by=actor.account.id, resumed_at=datetime.now(timezone.utc).isoformat())
            conn.execute("UPDATE backend_update_jobs SET status='running',result_json=? WHERE id=?", (json.dumps(plan), job_id))
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
            plan = json.loads(job['result_json'] or '{}')
            if job['kind'] == 'version' and job['status'] == 'awaiting_executor' and not plan.get('phase'):
                # Upgrade only work that has never crossed the installer boundary.
                plan['phase'] = 'core'
                with self.db.transaction() as conn:
                    conn.execute('UPDATE backend_update_jobs SET result_json=? WHERE id=?', (json.dumps(plan), job['id']))
                job = dict(job, result_json=json.dumps(plan))
            if job['kind'] == 'stack' or job['kind'] == 'version' and plan.get('phase') == 'core':
                if self._run_stack_core(job, actor):
                    return True
                if plan.get('phase') != 'agents':
                    continue
            if job['kind'] == 'version':
                # Legacy launches without receipts are observed, never replayed.
                state = self.updater.refresh_update_run_state()
                launch = json.loads(job['result_json'] or '{}')
                if (state.get('last_run_unit') != launch.get('unit_name')
                        or state.get('last_run_unit_missing') or state.get('last_run_observation_error')):
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
                    child_key = self._child_key(job, item)
                    if item['node_key'] == '@driver':
                        with self.db.transaction() as conn:
                            conn.execute("UPDATE backend_update_items SET status='running' WHERE job_id=? AND node_key='@driver'", (job['id'],))
                            conn.execute("UPDATE backend_update_jobs SET status='running' WHERE id=?", (job['id'],))
                        try:
                            root = Path(os.environ['NODE_PLANE_APP_DIR'])
                            args = ['bash', str(root / 'scripts/setup_driver_agents.sh'), '--skip-agents', '--bin-source', 'auto']
                            res = (self.runner(args) if self.runner else subprocess.run(args, cwd=root,
                                env=dict(os.environ), capture_output=True,
                                timeout=1200, check=False))
                            succeeded = res if isinstance(res, bool) else (res.returncode == 0)
                            if succeeded:
                                succeeded, _ = self._verify_commit(self.driver.binary_info, 'commit', intent['expected_commit'])
                                error_code = None if succeeded else 'driver_verification_failed'
                            else:
                                error_code = 'driver_install_failed'
                        except Exception:
                            succeeded = False
                            error_code = 'driver_install_unconfirmed'
                        with self.db.transaction() as conn:
                            conn.execute("UPDATE backend_update_items SET status=?,error_code=? WHERE job_id=? AND node_key='@driver'",
                                ('succeeded' if succeeded else 'blocked', error_code, job['id']))
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
            expected_unit_name = f"node-plane-update-{UUID(job['id'])}"
            plan.update(unit_name=expected_unit_name, launch_state='dispatching')
            with self.db.transaction() as conn:
                conn.execute("UPDATE backend_update_jobs SET status='running',result_json=? WHERE id=?", (json.dumps(plan), job['id']))
            intent = json.loads(job['intent_json'])
            try:
                binding = {'launch_job_id' if job['kind'] == 'version' else 'stack_job_id': job['id']}
                launched = self.updater.schedule_update(branch=intent['branch'],
                    target_ref=plan['resolved_ref'], **binding)
            except Exception:
                launched = {'status': 'failed'}
            if launched.get('unit_name') and launched['unit_name'] != expected_unit_name:
                launched = {'status': 'failed'}
                plan['error_code'] = 'update_unit_mismatch'
            if launched.get('launch_state') == 'not_started':
                plan['launch_state'] = 'not_started'
            if launched.get('status') == 'running':
                plan['launch_state'] = 'launched'
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
        if state.get('last_run_unit_missing'):
            # --collect removes completed transient units. Only the terminal
            # evidence for this exact job can replace the lost systemd status.
            progress = self._stack_progress(job['id'])
            if progress.get('status') == 'failed' and progress.get('rollback_status') in {'succeeded', 'failed'}:
                state = {**state, 'last_run_status': 'failed'}
            elif progress.get('status') == 'succeeded':
                state = {**state, 'last_run_status': 'success'}
            else:
                self._finish(job['id'], 'blocked', {**plan, 'error_code': 'update_verification_unavailable'})
                return True
            if not self.updater.confirm_collected_update(plan['unit_name'], state['last_run_status']):
                self._finish(job['id'], 'blocked', {**plan, 'error_code': 'update_verification_unavailable'})
                return True
            if progress.get('components'):
                plan['components'] = progress['components']
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
                plan['phase'] = 'done' if job['kind'] == 'version' else 'agents'
                plan.pop('error_code', None)
                plan.pop('rollback_status', None)
                self._finish(job['id'], 'succeeded' if job['kind'] == 'version' else 'running', plan)
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
            due = not last or datetime.fromisoformat(last.replace('Z', '+00:00')) < datetime.now(timezone.utc) - timedelta(minutes=app_settings.get_updates_check_interval_minutes())
        except (ValueError, TypeError):
            due = True
        if due:
            branch, track = app_settings.get_updates_branch(), app_settings.get_updates_dev_track()
            result = self.updater.check_for_updates()
            if (result.get('status') == 'available' and app_settings.is_updates_auto_check_enabled()
                and branch == app_settings.get_updates_branch() and track == app_settings.get_updates_dev_track()):
                self._notify_update(result, branch, track)

    def _notify_update(self, result, branch, track):
        from .alerts import AlertService
        version = str(result.get('remote_label') or result.get('remote_version') or '')[:200]
        ref = str(result.get('upstream_ref') or '')[:200]
        if not version:
            return
        identity = json.dumps([branch,track,ref,version])
        key = 'update_notification:' + hashlib.sha256(identity.encode()).hexdigest()
        with self.db.connect() as conn:
            if conn.execute('SELECT 1 FROM backend_system_settings WHERE key=?', (key,)).fetchone():
                return
        changelog = result.get('changelog')
        if not isinstance(changelog, str):
            from app.services.updates import get_release_changelog
            tag = ref if ref.startswith('v') else 'v' + str(result.get('remote_version') or '')
            changelog = get_release_changelog(tag) if track == 'tag' or branch == 'main' else ''
        payload = {'branch':branch, 'dev_track':track, 'version':version,
            'changelog':changelog.strip()[:65536]}
        import re
        repo = os.environ.get('NODE_PLANE_GITHUB_REPO','saharoktyan/node-plane')
        tag = ref if ref.startswith('v') else 'v' + str(result.get('remote_version') or '')
        if re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo) and re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?',tag):
            payload['release_url'] = f'https://github.com/{repo}/releases/tag/{tag}'
        timestamp = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as conn:
            conn.execute('UPDATE backend_account_guard SET revision=revision+1 WHERE id=1')
            from .maintenance_gate import active
            if active(conn):
                return
            inserted = conn.execute('''INSERT INTO backend_system_settings(key,value) VALUES (?,?)
                ON CONFLICT(key) DO NOTHING RETURNING key''', (key,json.dumps(payload))).fetchone()
            if inserted:
                AlertService._event(conn, {'key':'@controller','title':'Node Plane'},
                    'update_available', payload, False, timestamp)
