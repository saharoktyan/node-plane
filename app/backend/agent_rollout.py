"""Durable admin-requested rollout of the existing backend node agent installer."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from uuid import UUID, uuid4

from .authorization import AccessDenied, require_permission


SSH_TARGET = re.compile(r'(?:[A-Za-z_][A-Za-z0-9._-]*@)?(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])\Z')


class AgentRolloutService:
    def __init__(self, db, runner=None):
        self.db, self.runner = db, runner

    def initialize_schema(self):
        with self.db.transaction() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_agent_rollouts (
                id TEXT PRIMARY KEY, node_key TEXT NOT NULL,
                actor_account_id TEXT NOT NULL REFERENCES backend_accounts(id),
                command_key TEXT NOT NULL, intent_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('awaiting_executor', 'running', 'blocked', 'succeeded')),
                UNIQUE(actor_account_id, command_key)
            )''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_backend_agent_rollouts_work ON backend_agent_rollouts(status, id)')
            conn.execute('CREATE TABLE IF NOT EXISTS backend_agent_rollout_failures (task_id TEXT PRIMARY KEY, code TEXT NOT NULL)')

    @staticmethod
    def public(row):
        return {key: row[key] for key in ('id', 'node_key', 'status')}

    def request(self, actor, node_key, command_key, *, transport, ssh_target=None, ssh_port=22, install_rust=False):
        require_permission(actor, 'nodes.manage')
        if type(install_rust) is not bool:
            raise AccessDenied('invalid_input', 422)
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
        intent = {'transport': transport, 'ssh_target': ssh_target, 'ssh_port': ssh_port}
        if install_rust:
            intent['install_rust'] = True
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
        if row is None:
            raise AccessDenied('resource_not_found', 404)
        return {**self.public(row), 'failure_code': failure['code'] if failure else None}

    def recover(self):
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_agent_rollouts SET status = 'blocked' WHERE status = 'running'")

    def _execute(self, row):
        intent = json.loads(row['intent_json'])
        root = Path(os.environ['NODE_PLANE_APP_DIR'])
        script = root / 'scripts' / 'setup_driver_agents.sh'
        args = ['bash', str(script), '--backend-node-key', row['node_key'],
                '--bin-source', 'auto']
        if intent['transport'] == 'local':
            args.append('--backend-local')
        else:
            args += ['--backend-ssh-target', intent['ssh_target'],
                     '--backend-ssh-port', str(intent['ssh_port'])]
        if self.runner is not None:
            return self.runner(args)
        result = subprocess.run(args, cwd=root, capture_output=True, text=True,
            env={**os.environ, 'NODE_PLANE_INSTALL_RUST': 'yes' if intent.get('install_rust') else 'no'},
            timeout=1200, check=False)
        output = result.stdout + result.stderr
        if 'RUST_INSTALL_REQUIRED:' in output:
            self._failure_code = 'rust_required'
        elif 'Not enough free memory' in output or 'too busy' in output:
            self._failure_code = 'build_resources'
        return result.returncode == 0

    def run_one(self):
        with self.db.transaction() as conn:
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
