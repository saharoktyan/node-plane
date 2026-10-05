"""Revision-bound node settings commands and conservative driver outbox."""
from __future__ import annotations

import hashlib
import json
import re
from uuid import UUID, uuid4

from .authorization import AccessDenied, require_permission
from .nodes import _validate, protocol_defaults


def _key(value):
    try:
        return str(UUID(value))
    except (TypeError, ValueError, AttributeError):
        raise AccessDenied('invalid_idempotency_key', 422) from None


def _digest(intent):
    snapshot = {'protocols': sorted(intent['protocols']), 'settings': intent['settings']}
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _valid_result(intent, raw):
    try:
        result = json.loads(raw)
    except (TypeError, ValueError):
        return False
    return result == {'node_key': intent['node_key'], 'revision': intent['revision'],
                      'settings_sha256': _digest(intent)}


def _snapshot(node):
    protocols = json.loads(node['protocols_json'])
    settings = protocol_defaults(json.loads(node['settings_json']), protocols)
    if not protocols or 'public_host' not in settings or ('xray' in protocols and not {
        'xray_sni', 'xray_tcp_port', 'xray_xhttp_port', 'xray_xhttp_path'} <= set(settings)) or (
        'awg' in protocols and 'awg_port' not in settings):
        raise AccessDenied('node_settings_incomplete', 422)
    _validate({'settings': settings}, create=False)
    for field, value in settings.items():
        if not field.endswith('_port') and any(ord(char) < 33 or ord(char) > 126 for char in value):
            raise AccessDenied('invalid_input', 422)
        if field in {'public_host', 'xray_host', 'xray_sni', 'awg_public_host'} and (
            not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]', value)
            or '..' in value):
            raise AccessDenied('invalid_input', 422)
        if field == 'xray_xhttp_path' and not re.fullmatch(r'/[A-Za-z0-9/_~.%+-]*', value):
            raise AccessDenied('invalid_input', 422)
    if 'xray' in protocols and (settings['xray_tcp_port'] == settings['xray_xhttp_port']
                                or not settings['xray_xhttp_path'].startswith('/')):
        raise AccessDenied('invalid_input', 422)
    return {'node_key': node['key'], 'revision': node['desired_revision'],
            'protocols': protocols, 'settings': settings}


class NodeSettingsService:
    def __init__(self, db):
        self.db = db

    def initialize_schema(self):
        with self.db.transaction() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_node_settings_tasks (
                id TEXT PRIMARY KEY, node_key TEXT NOT NULL,
                actor_account_id TEXT NOT NULL REFERENCES backend_accounts(id),
                command_key TEXT NOT NULL, revision INTEGER NOT NULL,
                intent_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('awaiting_executor', 'running', 'blocked', 'succeeded', 'superseded')),
                result_json TEXT,
                UNIQUE(actor_account_id, command_key)
            )''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_backend_node_settings_work ON backend_node_settings_tasks(status, node_key)')

    @staticmethod
    def public(row):
        result = json.loads(row['result_json']) if row['result_json'] else {}
        return {'id': row['id'], 'node_key': row['node_key'], 'revision': row['revision'],
                'status': row['status'], **({'error_code': result['error_code']} if 'error_code' in result else {})}

    def get(self, actor, task_id):
        require_permission(actor, 'nodes.manage')
        with self.db.connect() as conn:
            row = conn.execute('SELECT * FROM backend_node_settings_tasks WHERE id = ?', (task_id,)).fetchone()
        if row is None:
            raise AccessDenied('resource_not_found', 404)
        return self.public(row)

    def queue(self, actor, node_key, *, revision, command_key):
        require_permission(actor, 'nodes.manage')
        key = _key(command_key)
        if type(revision) is not int or revision < 1:
            raise AccessDenied('invalid_revision', 422)
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            account = conn.execute('''UPDATE backend_accounts SET role = role WHERE id = ?
                RETURNING role, status''', (actor.account.id,)).fetchone()
            if account is None or account['role'] != 'admin' or account['status'] != 'approved':
                raise AccessDenied('permission_denied')
            previous = conn.execute('''SELECT * FROM backend_node_settings_tasks
                WHERE actor_account_id = ? AND command_key = ?''', (actor.account.id, key)).fetchone()
            if previous is not None:
                if previous['node_key'] != node_key or previous['revision'] != revision:
                    raise AccessDenied('idempotency_conflict', 409)
                return self.public(previous)
            node = conn.execute('''UPDATE backend_nodes SET enabled = enabled WHERE key = ?
                RETURNING *''', (node_key,)).fetchone()
            if node is None:
                raise AccessDenied('resource_not_found', 404)
            if conn.execute('SELECT 1 FROM backend_node_drains WHERE node_key = ?', (node_key,)).fetchone():
                raise AccessDenied('node_already_draining', 409)
            if node['desired_revision'] != revision:
                raise AccessDenied('revision_conflict', 412)
            if conn.execute("SELECT 1 FROM backend_node_jobs WHERE node_key = ? AND status IN ('awaiting_executor', 'running', 'blocked')", (node_key,)).fetchone():
                raise AccessDenied('node_operation_pending', 409)
            if node['applied_revision'] >= revision:
                raise AccessDenied('settings_already_applied', 409)
            intent = _snapshot(node)
            if conn.execute('''SELECT 1 FROM backend_node_settings_tasks WHERE node_key = ?
                AND status IN ('awaiting_executor', 'running', 'blocked')''', (node_key,)).fetchone():
                raise AccessDenied('node_settings_operation_pending', 409)
            task_id = str(uuid4())
            conn.execute('''INSERT INTO backend_node_settings_tasks
                (id, node_key, actor_account_id, command_key, revision, intent_json, status)
                VALUES (?, ?, ?, ?, ?, ?, 'awaiting_executor')''',
                (task_id, node_key, actor.account.id, key, revision,
                 json.dumps(intent, sort_keys=True, separators=(',', ':'))))
            return {'id': task_id, 'node_key': node_key, 'revision': revision,
                    'status': 'awaiting_executor'}


class NodeSettingsExecutor:
    def __init__(self, db, driver):
        self.db, self.driver = db, driver

    def recover(self):
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_node_settings_tasks SET status = 'blocked' WHERE status = 'running'")

    def resolve_blocked(self, actor, task_id):
        """Retire an uncertain command after agent restart and queue a fresh revision.

        Caller holds the same process lock as the executor. Agent retirement
        precedes the DB transaction so a failed commit can safely retry.
        """
        require_permission(actor, 'maintenance.manage')
        with self.db.connect() as conn:
            row = conn.execute('SELECT * FROM backend_node_settings_tasks WHERE id = ?',
                               (task_id,)).fetchone()
            if row is None:
                raise AccessDenied('resource_not_found', 404)
            if row['status'] != 'blocked':
                raise AccessDenied('task_not_blocked', 409)
            intent = json.loads(row['intent_json'])
            node = conn.execute('SELECT * FROM backend_nodes WHERE key = ?', (row['node_key'],)).fetchone()
            if node is None:
                raise AccessDenied('resource_not_found', 404)
            _snapshot(node)
        observation = self.driver.resolve_node_settings(task_id, intent)
        if set(observation) != {'config_matches', 'containers_running'} or any(
                type(value) is not bool for value in observation.values()):
            raise ValueError('invalid repair observation')
        with self.db.transaction() as conn:
            account = conn.execute('''UPDATE backend_accounts SET role = role WHERE id = ?
                RETURNING role, status''', (actor.account.id,)).fetchone()
            if account is None or account['role'] != 'admin' or account['status'] != 'approved':
                raise AccessDenied('permission_denied')
            current = conn.execute('SELECT * FROM backend_node_settings_tasks WHERE id = ? AND status = ?',
                                   (task_id, 'blocked')).fetchone()
            node = conn.execute('''UPDATE backend_nodes SET enabled = 0 WHERE key = ?
                RETURNING *''', (row['node_key'],)).fetchone()
            if current is None or node is None:
                raise AccessDenied('repair_conflict', 409)
            if conn.execute('SELECT 1 FROM backend_node_drains WHERE node_key = ?', (row['node_key'],)).fetchone():
                raise AccessDenied('node_already_draining', 409)
            if conn.execute('''SELECT 1 FROM backend_node_settings_tasks WHERE node_key = ?
                AND id != ? AND status IN ('awaiting_executor', 'running', 'blocked')''',
                            (row['node_key'], task_id)).fetchone():
                raise AccessDenied('node_settings_operation_pending', 409)
            if conn.execute('''SELECT 1 FROM backend_operation_tasks WHERE node_key = ?
                AND status IN ('running', 'blocked')''', (row['node_key'],)).fetchone():
                raise AccessDenied('profile_operation_uncertain', 409)
            revision = max(node['desired_revision'], intent['revision']) + 1
            conn.execute('UPDATE backend_nodes SET desired_revision = ? WHERE key = ?',
                         (revision, row['node_key']))
            fresh = _snapshot(dict(node, desired_revision=revision))
            next_id = str(uuid4())
            conn.execute("UPDATE backend_node_settings_tasks SET status = 'superseded', result_json = ? WHERE id = ?",
                         (json.dumps({'repair_observation': observation}, sort_keys=True), task_id))
            conn.execute('''INSERT INTO backend_node_settings_tasks
                (id, node_key, actor_account_id, command_key, revision, intent_json, status)
                VALUES (?, ?, ?, ?, ?, ?, 'awaiting_executor')''',
                (next_id, row['node_key'], actor.account.id, str(uuid4()), revision,
                 json.dumps(fresh, sort_keys=True, separators=(',', ':'))))
        return {'task_id': next_id, 'revision': revision, 'observation': observation}

    @staticmethod
    def _finish(conn, row, result):
        intent = json.loads(row['intent_json'])
        if not _valid_result(intent, result):
            conn.execute("UPDATE backend_node_settings_tasks SET status = 'blocked' WHERE id = ?", (row['id'],))
            return False
        conn.execute("UPDATE backend_node_settings_tasks SET status = 'succeeded', result_json = ? WHERE id = ?",
                     (result, row['id']))
        conn.execute('''UPDATE backend_nodes SET applied_revision = ?,
            enabled = CASE WHEN desired_revision = ? THEN 1 ELSE enabled END
            WHERE key = ? AND applied_revision < ? AND desired_revision >= ?
            AND NOT EXISTS (SELECT 1 FROM backend_node_drains WHERE node_key = ?)''',
            (intent['revision'], intent['revision'], row['node_key'],
             intent['revision'], intent['revision'], row['node_key']))
        return True

    def reconcile_completed(self):
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM backend_node_settings_tasks WHERE status = 'blocked' ORDER BY id").fetchall()
        count = 0
        for row in rows:
            try:
                result = self.driver.recover_node_settings(row['id'], json.loads(row['intent_json']))
            except Exception:
                continue
            with self.db.transaction() as conn:
                current = conn.execute("SELECT * FROM backend_node_settings_tasks WHERE id = ? AND status = 'blocked'", (row['id'],)).fetchone()
                if current is not None and self._finish(conn, current, result):
                    count += 1
        return count

    def run_one(self):
        with self.db.connect() as conn:
            rows = conn.execute('''SELECT * FROM backend_node_settings_tasks
                WHERE status = 'awaiting_executor' AND NOT EXISTS (
                    SELECT 1 FROM backend_operation_tasks p
                    WHERE p.node_key = backend_node_settings_tasks.node_key
                      AND p.status IN ('running', 'blocked'))
                ORDER BY id''').fetchall()
        row = None
        for candidate in rows:
            with self.db.connect() as conn:
                node = conn.execute('SELECT desired_revision,applied_revision FROM backend_nodes WHERE key = ?', (candidate['node_key'],)).fetchone()
            if node is None or node['desired_revision'] != candidate['revision']:
                with self.db.transaction() as conn:
                    conn.execute("UPDATE backend_node_settings_tasks SET status = 'superseded' WHERE id = ? AND status = 'awaiting_executor'", (candidate['id'],))
                return True
            try:
                observation = self.driver.inspect_node(candidate['node_key'])
                protocols = json.loads(candidate['intent_json'])['protocols']
                if (observation['health_state'] != 'running' or
                    ('xray' in protocols and not observation['xray_config_present']) or
                    ('awg' in protocols and not observation['awg_config_present'])):
                    if not node['applied_revision']:
                        self._retire_uninstalled(candidate)
                        return True
                    # Preparation is repeatable and occurs before the durable
                    # settings mutation. No command is claimed on failure.
                    self.driver.prepare_node(candidate['node_key'])
            except Exception:
                if not node['applied_revision']:
                    self._retire_uninstalled(candidate)
                    return True
                # An absent agent or incomplete preparation leaves the task
                # queued; the settings command was not started.
                continue
            with self.db.transaction() as conn:
                current = conn.execute("SELECT * FROM backend_node_settings_tasks WHERE id = ? AND status = 'awaiting_executor'", (candidate['id'],)).fetchone()
                current_node = conn.execute('SELECT desired_revision FROM backend_nodes WHERE key = ?', (candidate['node_key'],)).fetchone()
                draining = conn.execute('SELECT 1 FROM backend_node_drains WHERE node_key = ?', (candidate['node_key'],)).fetchone()
                if current is None:
                    continue
                if current_node is None or current_node['desired_revision'] != candidate['revision'] or draining:
                    conn.execute("UPDATE backend_node_settings_tasks SET status = 'superseded' WHERE id = ?", (candidate['id'],))
                    return True
                claimed = conn.execute("UPDATE backend_node_settings_tasks SET status = 'running' WHERE id = ? AND status = 'awaiting_executor' RETURNING id", (candidate['id'],)).fetchone()
                if claimed is None:
                    continue
            row = candidate
            break
        if row is None:
            return False
        try:
            result = self.driver.apply_node_settings(row['id'], json.loads(row['intent_json']))
        except Exception:
            result = None
        with self.db.transaction() as conn:
            current = conn.execute("SELECT * FROM backend_node_settings_tasks WHERE id = ? AND status = 'running'", (row['id'],)).fetchone()
            if current is not None:
                self._finish(conn, current, result)
        return True

    def _retire_uninstalled(self, candidate):
        # No settings mutation was claimed. Bootstrap can safely take over.
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_node_settings_tasks SET status='superseded', result_json=? WHERE id=? AND status='awaiting_executor'",
                (json.dumps({'error_code': 'node_installation_required'}), candidate['id']))
