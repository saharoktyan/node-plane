"""Durable admin-requested agent installation on the controller's own host, and
agent updates. Remote servers enroll with an install command and update
themselves from signed releases; the controller has no SSH path to them."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import time
from uuid import UUID, uuid4

from .authorization import AccessDenied, require_permission


# Longer than the agent's rollback guard, so a failed start is seen as rolled back.
SELF_UPDATE_WAIT = 300
SELF_UPDATE_POLL = 5
# What the agent reports, as the failure codes operators see.
SELF_UPDATE_FAILURES = {
    'agent_update_signature_invalid': 'agent_update_rejected',
    'agent_update_statement_invalid': 'agent_update_rejected',
    'agent_update_checksum_mismatch': 'agent_update_rejected',
    'agent_update_version_mismatch': 'agent_update_rejected',
    'agent_update_download_failed': 'agent_update_download_failed',
    'agent_update_downloader_missing': 'agent_update_download_failed',
    'agent_update_self_check_failed': 'agent_update_self_check_failed',
    'agent_update_downgrade': 'agent_update_downgrade',
    'agent_update_rolled_back': 'agent_update_rolled_back',
}


SSH_TARGET = re.compile(r'(?:[A-Za-z_][A-Za-z0-9._-]*@)?(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])\Z')


class AgentRolloutService:
    def __init__(self, db, runner=None, driver=None, release=None, sleep=time.sleep, clock=time.monotonic):
        self.db, self.runner, self.driver = db, runner, driver
        self.release, self.sleep, self.clock = release, sleep, clock

    def initialize_schema(self):
        with self.db.transaction() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_agent_rollouts (
                id TEXT PRIMARY KEY, node_key TEXT NOT NULL,
                actor_account_id TEXT NOT NULL REFERENCES backend_accounts(id),
                command_key TEXT NOT NULL, intent_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('awaiting_executor', 'running', 'blocked', 'succeeded', 'cancelled')),
                UNIQUE(actor_account_id, command_key)
            )''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_backend_agent_rollouts_work ON backend_agent_rollouts(status, id)')
            conn.execute('CREATE TABLE IF NOT EXISTS backend_agent_rollout_failures (task_id TEXT PRIMARY KEY, code TEXT NOT NULL)')
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_agent_rollout_archives (
                task_id TEXT NOT NULL, path TEXT NOT NULL, PRIMARY KEY(task_id, path))''')

    @staticmethod
    def public(row):
        return {key: row[key] for key in ('id', 'node_key', 'status')}

    def request(self, actor, node_key, command_key, *, transport, ssh_target=None, ssh_port=22, install_rust=False, skip_driver=False, bootstrap_id=None, self_update=False):
        require_permission(actor, 'nodes.manage')
        if type(install_rust) is not bool or type(skip_driver) is not bool or type(self_update) is not bool:
            raise AccessDenied('invalid_input', 422)
        if install_rust:
            raise AccessDenied('install_rust_unsupported', 422)
        try:
            key = str(UUID(command_key))
        except (TypeError, ValueError, AttributeError):
            raise AccessDenied('invalid_idempotency_key', 422) from None
        if transport not in {'local', 'ssh'} or type(ssh_port) is not int or not 1 <= ssh_port <= 65535:
            raise AccessDenied('invalid_input', 422)
        if transport == 'local':
            if ssh_target is not None or ssh_port != 22:
                raise AccessDenied('invalid_input', 422)
        elif not isinstance(ssh_target, str) or not SSH_TARGET.fullmatch(ssh_target):
            raise AccessDenied('invalid_input', 422)
        # A remote server is only updated, by its agent's signed self-update.
        if transport != 'local' and not self_update:
            raise AccessDenied('agent_install_command_required', 409)
        intent = {'transport': transport, 'ssh_target': ssh_target, 'ssh_port': ssh_port}
        if skip_driver:
            intent['skip_driver'] = True
        # Only updates try the agent's own signed update; setup and repair
        # always run the installer.
        if self_update:
            intent['self_update'] = True
        encoded = json.dumps(intent, sort_keys=True, separators=(',', ':'))
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            account = conn.execute('''UPDATE backend_accounts SET role = role WHERE id = ?
                RETURNING role, status''', (actor.account.id,)).fetchone()
            if account is None or account['role'] != 'admin' or account['status'] != 'approved':
                raise AccessDenied('permission_denied')
            previous = conn.execute('''SELECT * FROM backend_agent_rollouts
                WHERE actor_account_id = ? AND command_key = ?''', (actor.account.id, key)).fetchone()
            if previous is not None:
                if previous['node_key'] != node_key or previous['intent_json'] != encoded:
                    raise AccessDenied('idempotency_conflict', 409)
                return self.public(previous)
            node = conn.execute('SELECT key FROM backend_nodes WHERE key = ?', (node_key,)).fetchone()
            if node is None:
                raise AccessDenied('resource_not_found', 404)
            connection = conn.execute('''SELECT transport, ssh_target FROM backend_node_connections
                WHERE node_key = ?''', (node_key,)).fetchone()
            if connection is not None and (connection['transport'] != transport
                                           or connection['ssh_target'] != ssh_target):
                raise AccessDenied('node_connection_mismatch', 409)
            from .node_bootstrap import require_idle
            require_idle(conn, node_key, bootstrap_id)
            if conn.execute('''SELECT 1 FROM backend_node_drains WHERE node_key = ?''',
                            (node_key,)).fetchone():
                raise AccessDenied('node_already_draining', 409)
            if conn.execute('''SELECT 1 FROM backend_agent_rollouts WHERE node_key = ?
                AND status IN ('awaiting_executor', 'running')''', (node_key,)).fetchone():
                raise AccessDenied('agent_rollout_pending', 409)
            task_id = str(uuid4())
            conn.execute('''INSERT INTO backend_agent_rollouts
                (id, node_key, actor_account_id, command_key, intent_json, status)
                VALUES (?, ?, ?, ?, ?, 'awaiting_executor')''',
                (task_id, node_key, actor.account.id, key, encoded))
            return {'id': task_id, 'node_key': node_key, 'status': 'awaiting_executor'}

    def get(self, actor, task_id):
        require_permission(actor, 'nodes.manage')
        with self.db.connect() as conn:
            row = conn.execute('SELECT * FROM backend_agent_rollouts WHERE id = ?', (task_id,)).fetchone()
            failure = conn.execute('SELECT code FROM backend_agent_rollout_failures WHERE task_id = ?', (task_id,)).fetchone()
            archives = conn.execute('SELECT path FROM backend_agent_rollout_archives WHERE task_id=? ORDER BY path', (task_id,)).fetchall()
        if row is None:
            raise AccessDenied('resource_not_found', 404)
        return {**self.public(row), 'failure_code': failure['code'] if failure else None,
                'journal_archives': [item['path'] for item in archives],
                'progress': self.progress(row)}

    @staticmethod
    def progress(row):
        from .installation_progress import read_progress
        return read_progress(row)

    def recover(self):
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_agent_rollouts SET status = 'blocked' WHERE status = 'running'")

    def preflight_failed(self, row):
        """Only a confirmed safe boundary permits another installer dispatch.

        Binary resolution and a refused self-update finish before agent
        identity or journals are modified. A typed failure code is recorded
        only after the subprocess returns; interrupted/unknown executions never
        acquire this permission.
        """
        with self.db.connect() as conn:
            failure = conn.execute('SELECT code FROM backend_agent_rollout_failures WHERE task_id=?', (row['id'],)).fetchone()
            node = conn.execute('SELECT key FROM backend_nodes WHERE key=?', (row['node_key'],)).fetchone()
            abandoned = conn.execute("SELECT 1 FROM backend_node_bootstraps WHERE child_id=? AND status='superseded'", (row['id'],)).fetchone()
        return bool(row['status'] == 'blocked' and node and not abandoned and failure
                    and failure['code'] in {'release_binary_unavailable', 'rust_required',
                                            'agent_update_rejected', 'agent_update_download_failed',
                                            'agent_update_self_check_failed'})

    def resume_preflight(self, actor, task_id):
        require_permission(actor, 'maintenance.manage')
        require_permission(actor, 'nodes.manage')
        with self.db.connect() as conn:
            row = conn.execute('SELECT * FROM backend_agent_rollouts WHERE id=?', (task_id,)).fetchone()
        if row is None or not self.preflight_failed(row):
            raise AccessDenied('recovery_unconfirmed', 409)
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            account = conn.execute('SELECT role,status FROM backend_accounts WHERE id=?', (actor.account.id,)).fetchone()
            if not account or account['role'] != 'admin' or account['status'] != 'approved':
                raise AccessDenied('permission_denied')
            connection = conn.execute('SELECT transport,ssh_target FROM backend_node_connections WHERE node_key=?', (row['node_key'],)).fetchone()
            intent = json.loads(row['intent_json'])
            if (not connection or (connection['transport'], connection['ssh_target']) !=
                    (intent['transport'], intent['ssh_target']) or
                    conn.execute('SELECT 1 FROM backend_node_drains WHERE node_key=?', (row['node_key'],)).fetchone()):
                raise AccessDenied('repair_conflict', 409)
            conn.execute("UPDATE backend_agent_rollouts SET status='awaiting_executor' WHERE id=? AND status='blocked'", (task_id,))
            conn.execute('DELETE FROM backend_agent_rollout_failures WHERE task_id=?', (task_id,))
        return self.get(actor, task_id)

    @staticmethod
    def _installer_args(row):
        intent = json.loads(row['intent_json'])
        root = Path(os.environ['NODE_PLANE_APP_DIR'])
        script = root / 'scripts' / 'setup_driver_agents.sh'
        args = ['bash', str(script), '--backend-node-key', row['node_key'],
                '--bin-source', 'auto']
        if intent.get('skip_driver'):
            args.append('--skip-driver')
        args.append('--backend-local')
        return root, args

    def _self_update(self, row):
        """Update a running agent to this controller's signed release.

        Returns None when the agent or the release cannot do that, so the
        installer runs instead; otherwise whether the update succeeded.
        """
        if self.driver is None or not json.loads(row['intent_json']).get('self_update'):
            return None
        from .agent_enrollment import release_assets
        from .driver_transport import AgentNotConfigured, AgentUpdateUnsupported
        try:
            release = (self.release or release_assets)()
        except AccessDenied:
            return None
        if not release.get('signed'):
            return None
        version = release['tag'][1:]
        try:
            answer = self.driver.update_agent(row['node_key'], release['base'], version)
        except (AgentNotConfigured, AgentUpdateUnsupported):
            return None
        except Exception as exc:
            details = exc.details() if callable(getattr(exc, 'details', None)) else ''
            self._failure_code = SELF_UPDATE_FAILURES.get(details, 'agent_update_failed')
            return False
        if answer['status'] == 'current':
            return True
        deadline = self.clock() + SELF_UPDATE_WAIT
        while self.clock() < deadline:
            self.sleep(SELF_UPDATE_POLL)
            try:
                facts = self.driver.inspect_node(row['node_key'])
            except Exception:
                continue
            if facts.get('agent_version') == version:
                return True
            if facts.get('agent_update_rolled_back') == version:
                self._failure_code = 'agent_update_rolled_back'
                return False
        self._failure_code = 'agent_update_unconfirmed'
        return False

    def _execute(self, row):
        updated = self._self_update(row)
        if updated is not None:
            return updated
        if json.loads(row['intent_json'])['transport'] != 'local':
            # An agent from before self-update: only a new install command
            # replaces it.
            self._failure_code = 'agent_update_requires_reinstall'
            return False
        root, args = self._installer_args(row)
        if self.runner is not None:
            return self.runner(args)
        from .installation_progress import progress_path
        env = dict(os.environ)
        # Override inherited observer targets; every job owns its own file.
        env.pop('NODE_PLANE_AGENT_PROGRESS_FILE', None)
        env.pop('NODE_PLANE_AGENT_PROGRESS_ID', None)
        env.pop('NODE_PLANE_INSTALL_RUST', None)
        path = progress_path(row['id']) if 'id' in row.keys() else None
        if path is not None:
            env.update(NODE_PLANE_AGENT_PROGRESS_FILE=str(path), NODE_PLANE_AGENT_PROGRESS_ID=row['id'])
        result = subprocess.run(args, cwd=root, capture_output=True, text=True,
            env=env,
            timeout=1200, check=False)
        output = result.stdout + result.stderr
        for line in output.splitlines():
            parts = line.split('|', 2)
            if (len(parts) == 3 and parts[0] == 'AGENT_JOURNAL_ARCHIVE'
                    and parts[1] == row['node_key'] and parts[2].startswith('/')
                    and '/journal-archives/previous-controller-' in parts[2]
                    and len(parts[2]) <= 4096 and not any(c in parts[2] for c in '\r\0')):
                with self.db.transaction() as conn:
                    conn.execute('''INSERT INTO backend_agent_rollout_archives VALUES (?, ?)
                        ON CONFLICT(task_id, path) DO NOTHING''', (row['id'], parts[2]))
        if (re.search(r'(?m)^(?:RELEASE_BINARY_UNAVAILABLE: )?(?:Driver|Agent) release asset [^\r\n]+ is missing or inaccessible\.', output)
                or re.search(r'(?m)^RELEASE_BINARY_UNAVAILABLE: (?:'
                             r'(?:Driver|Agent) release artifact verification failed for [^\r\n]+\.|'
                             r'Failed to parse (?:driver|agent) checksum for [^\r\n]+ from SHA256SUMS\.txt|'
                             r'Release checksum asset SHA256SUMS\.txt for [^\r\n]+ is missing or inaccessible\.)', output)):
            self._failure_code = 'release_binary_unavailable'
        elif 'RUST_INSTALL_REQUIRED:' in output:
            self._failure_code = 'rust_required'
        elif 'Not enough free memory' in output or 'too busy' in output:
            self._failure_code = 'build_resources'
        elif 'AGENT_JOURNAL_OWNER_CONFLICT:' in output:
            self._failure_code = 'agent_journal_owner_conflict'
        return result.returncode == 0

    def run_one(self):
        with self.db.transaction() as conn:
            conn.execute('UPDATE backend_account_guard SET revision=revision+1 WHERE id=1')
            from .maintenance_gate import core_upgrade
            if core_upgrade(conn):
                return False
            row = conn.execute('''SELECT * FROM backend_agent_rollouts
                WHERE status = 'awaiting_executor' ORDER BY id LIMIT 1''').fetchone()
            if row is None:
                return False
            conn.execute("UPDATE backend_agent_rollouts SET status = 'running' WHERE id = ?",
                         (row['id'],))
        try:
            self._failure_code = None
            succeeded = self._execute(row)
        except Exception:
            succeeded = False
        with self.db.transaction() as conn:
            if self._failure_code:
                conn.execute('INSERT INTO backend_agent_rollout_failures VALUES (?, ?) ON CONFLICT(task_id) DO UPDATE SET code=excluded.code', (row['id'], self._failure_code))
            conn.execute('''UPDATE backend_agent_rollouts SET status = ?
                WHERE id = ? AND status = 'running' ''',
                ('succeeded' if succeeded else 'blocked', row['id']))
        return True
