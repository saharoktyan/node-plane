"""Transactional outbox; desired state does not imply remote success.

Tasks are processed by the separate explicit driver executor. Private snapshots are never
included in the operation HTTP representation.
"""
from datetime import datetime, timezone
import json
import secrets
from uuid import uuid4

from .authorization import AccessDenied, require_permission


class OperationRepository:
    def __init__(self, db):
        self.db = db

    def initialize_schema(self):
        with self.db.transaction() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS backend_operations (
                id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES backend_profiles(id),
                actor_id TEXT NOT NULL REFERENCES backend_accounts(id),
                desired_revision INTEGER NOT NULL, status TEXT NOT NULL
                    CHECK(status IN ('no_targets', 'awaiting_executor', 'running', 'succeeded', 'blocked', 'superseded')),
                created_at TEXT NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS backend_operation_tasks (
                id TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES backend_operations(id),
                node_key TEXT NOT NULL, protocol TEXT NOT NULL CHECK(protocol IN ('awg', 'xray')),
                action TEXT NOT NULL CHECK(action IN ('ensure', 'delete')),
                status TEXT NOT NULL CHECK(status IN ('awaiting_executor', 'running', 'succeeded', 'blocked', 'superseded')),
                intent_json TEXT NOT NULL, result_json TEXT, driver_operation_id TEXT,
                inspection_json TEXT, inspected_at TEXT,
                UNIQUE(operation_id, node_key, protocol)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS backend_profile_identities (
                profile_id TEXT PRIMARY KEY REFERENCES backend_profiles(id),
                xray_uuid TEXT NOT NULL, xray_short_id TEXT NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS backend_repairs (
                task_id TEXT PRIMARY KEY REFERENCES backend_operation_tasks(id),
                actor_id TEXT NOT NULL REFERENCES backend_accounts(id),
                operation_id TEXT NOT NULL REFERENCES backend_operations(id),
                observation_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""")
            conn.execute('CREATE INDEX IF NOT EXISTS backend_operations_profile ON backend_operations(profile_id, desired_revision)')

    @staticmethod
    def targets(conn, profile_id):
        # A removed grant may still exist remotely, including when an earlier
        # intent has not been dispatched. Keep historical targets for revocation.
        grants = conn.execute('SELECT node_key, protocol FROM backend_grants WHERE profile_id = ?', (profile_id,)).fetchall()
        tasks = conn.execute("""SELECT t.node_key, t.protocol FROM backend_operation_tasks t
            JOIN backend_operations o ON o.id = t.operation_id WHERE o.profile_id = ?""", (profile_id,)).fetchall()
        return {(r['node_key'], r['protocol']) for r in [*grants, *tasks]}

    @staticmethod
    def record(conn, actor, profile_id, previous_targets):
        profile = conn.execute('SELECT * FROM backend_profiles WHERE id = ?', (profile_id,)).fetchone()
        grants = conn.execute('SELECT node_key, protocol FROM backend_grants WHERE profile_id = ?', (profile_id,)).fetchall()
        current_targets = {(r['node_key'], r['protocol']) for r in grants}
        targets = previous_targets | current_targets
        now = datetime.now(timezone.utc)
        active = not profile['frozen'] and (profile['expires_at'] is None or datetime.fromisoformat(profile['expires_at']) > now)
        conn.execute("""INSERT INTO backend_profile_identities(profile_id, xray_uuid, xray_short_id)
            VALUES (?, ?, ?) ON CONFLICT(profile_id) DO NOTHING""", (profile_id, str(uuid4()), secrets.token_hex(8)))
        identity = conn.execute('SELECT xray_uuid, xray_short_id FROM backend_profile_identities WHERE profile_id = ?', (profile_id,)).fetchone()
        operation_id = str(uuid4())
        actionable_targets = []
        for node_key, protocol in sorted(targets):
            conn.execute('UPDATE backend_nodes SET enabled = enabled WHERE key = ?', (node_key,))
            # Once cleanup starts, the agent has a durable mutation fence.
            # Later edits of this profile must not create impossible work for
            # that retired node. Historical tasks remain in the audit log.
            if (not conn.execute('SELECT 1 FROM backend_node_cleanup WHERE node_key = ?', (node_key,)).fetchone()
                    and not conn.execute('SELECT 1 FROM backend_node_retirements WHERE node_key = ?', (node_key,)).fetchone()):
                actionable_targets.append((node_key, protocol))
        status = 'awaiting_executor' if actionable_targets else 'no_targets'
        conn.execute("""INSERT INTO backend_operations(id, profile_id, actor_id, desired_revision, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""", (operation_id, profile_id, actor.account.id, profile['desired_revision'], status, now.isoformat()))
        for node_key, protocol in actionable_targets:
            node = conn.execute('SELECT enabled FROM backend_nodes WHERE key = ?', (node_key,)).fetchone()
            action = 'ensure' if active and node is not None and node['enabled'] and (node_key, protocol) in current_targets else 'delete'
            intent = {'version': 1, 'profile_id': profile_id, 'runtime_name': profile['runtime_name'],
                      'desired_revision': profile['desired_revision'], 'node_key': node_key,
                      'protocol': protocol, 'action': action, 'expires_at': profile['expires_at']}
            if protocol == 'xray' and action == 'ensure':
                intent['xray'] = {'uuid': identity['xray_uuid'], 'short_id': identity['xray_short_id']}
            conn.execute("""INSERT INTO backend_operation_tasks(id, operation_id, node_key, protocol, action, status, intent_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)""", (str(uuid4()), operation_id, node_key, protocol, action, 'awaiting_executor', json.dumps(intent, sort_keys=True)))
        return {'id': operation_id, 'status': status}

    def get(self, actor, operation_id):
        require_permission(actor, 'operations.read')
        with self.db.connect() as conn:
            row = conn.execute('SELECT * FROM backend_operations WHERE id = ?', (operation_id,)).fetchone()
            if row is None:
                raise AccessDenied('resource_not_found', 404)
            if row['actor_id'] != actor.account.id:
                try:
                    require_permission(actor, 'profiles.manage')
                except AccessDenied:
                    raise AccessDenied('resource_not_found', 404) from None
            tasks = conn.execute('SELECT id, node_key, protocol, action, status, inspection_json, inspected_at FROM backend_operation_tasks WHERE operation_id = ? ORDER BY node_key, protocol', (operation_id,)).fetchall()
            return {field: row[field] for field in ('id', 'profile_id', 'desired_revision', 'status', 'created_at')} | {'tasks': [{**{key: t[key] for key in ('id', 'node_key', 'protocol', 'action', 'status', 'inspected_at')}, 'inspection': json.loads(t['inspection_json']) if t['inspection_json'] else None} for t in tasks]}
