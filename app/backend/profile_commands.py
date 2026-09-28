"""Atomic desired-state commands and durable runtime intent outbox."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID, uuid4

from .authorization import AccessDenied, require_permission
from .profiles import ProfileRepository
from .operations import OperationRepository


class ProfileCommands:
    def __init__(self, db):
        self.db = db

    def initialize_schema(self):
        OperationRepository(self.db).initialize_schema()
        with self.db.transaction() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_profile_commands (
                principal_id TEXT NOT NULL, actor_id TEXT NOT NULL, command_key TEXT NOT NULL,
                fingerprint TEXT NOT NULL, result_json TEXT,
                PRIMARY KEY(principal_id, actor_id, command_key)
            )''')

    @staticmethod
    def result(conn, profile_id):
        row = conn.execute('SELECT * FROM backend_profiles WHERE id = ?', (profile_id,)).fetchone()
        grants = conn.execute('SELECT node_key, protocol FROM backend_grants WHERE profile_id = ? ORDER BY node_key, protocol', (profile_id,)).fetchall()
        return {'profile': ProfileRepository.public(row),
                'grants': [{'node_key': r['node_key'], 'protocol': r['protocol']} for r in grants]}

    def execute(self, actor, key, *, action, profile_id=None, revision=None, values=None):
        try:
            key = str(UUID(key))
        except (TypeError, ValueError, AttributeError):
            raise AccessDenied('invalid_idempotency_key', 422) from None
        require_permission(actor, 'grants.manage' if action == 'grants' else 'profiles.manage')
        if action not in {'create', 'edit', 'grants'}:
            raise ValueError('unknown profile command')
        values = values or {}
        fingerprint = hashlib.sha256(json.dumps({'action': action, 'profile_id': profile_id,
            'revision': revision, 'values': values}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        identity = (actor.principal.id, actor.account.id, key)
        with self.db.transaction() as conn:
            conn.execute('''INSERT INTO backend_profile_commands(principal_id, actor_id, command_key, fingerprint)
                VALUES (?, ?, ?, ?) ON CONFLICT(principal_id, actor_id, command_key) DO NOTHING''', (*identity, fingerprint))
            record = conn.execute('''SELECT fingerprint, result_json FROM backend_profile_commands
                WHERE principal_id = ? AND actor_id = ? AND command_key = ?''', identity).fetchone()
            if record['fingerprint'] != fingerprint:
                raise AccessDenied('idempotency_conflict', 409)
            if record['result_json'] is not None:
                return json.loads(record['result_json'])
            previous_targets = set()
            if profile_id is not None:
                previous_targets = OperationRepository.targets(conn, profile_id)
            if action == 'create':
                if set(values) - {'display_name', 'owner_account_id'}:
                    raise AccessDenied('invalid_input', 422)
                display_name = values.get('display_name')
                if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 128:
                    raise AccessDenied('invalid_input', 422)
                owner = values.get('owner_account_id')
                if owner is not None and conn.execute('SELECT id FROM backend_accounts WHERE id = ?', (owner,)).fetchone() is None:
                    raise AccessDenied('owner_not_found', 422)
                profile_id = str(uuid4())
                conn.execute('INSERT INTO backend_profiles(id, runtime_name, display_name, owner_account_id) VALUES (?, ?, ?, ?)',
                             (profile_id, 'p_' + uuid4().hex, display_name.strip(), owner))
            else:
                if revision is None:
                    raise AccessDenied('revision_required', 428)
                row = conn.execute('SELECT desired_revision FROM backend_profiles WHERE id = ?', (profile_id,)).fetchone()
                if row is None:
                    raise AccessDenied('resource_not_found', 404)
                if type(revision) is not int or revision < 1:
                    raise AccessDenied('invalid_revision', 422)
                if row['desired_revision'] != revision:
                    raise AccessDenied('revision_conflict', 412)
                if action == 'edit':
                    if not values or set(values) - {'display_name', 'frozen', 'expires_at'}:
                        raise AccessDenied('invalid_input', 422)
                    if 'display_name' in values:
                        name = values['display_name']
                        if not isinstance(name, str) or not name.strip() or len(name) > 128:
                            raise AccessDenied('invalid_input', 422)
                        values = {**values, 'display_name': name.strip()}
                    if 'frozen' in values and type(values['frozen']) is not bool:
                        raise AccessDenied('invalid_input', 422)
                    if values.get('expires_at') is not None:
                        try:
                            expires = datetime.fromisoformat(values['expires_at'])
                            if expires.tzinfo is None:
                                raise ValueError()
                            values = {**values, 'expires_at': expires.astimezone(timezone.utc).isoformat()}
                        except (ValueError, TypeError):
                            raise AccessDenied('invalid_input', 422) from None
                    assignments = ', '.join(f'{field} = ?' for field in sorted(values))
                    parameters = [int(values[field]) if field == 'frozen' else values[field] for field in sorted(values)]
                    cursor = conn.execute(f'UPDATE backend_profiles SET {assignments}, desired_revision = desired_revision + 1 WHERE id = ? AND desired_revision = ? RETURNING id',
                                         (*parameters, profile_id, revision))
                else:
                    grants = values.get('grants')
                    if set(values) != {'grants'} or not isinstance(grants, list) or len(grants) > 100:
                        raise AccessDenied('invalid_input', 422)
                    seen = set()
                    for grant in grants:
                        if not isinstance(grant, dict) or set(grant) != {'node_key', 'protocol'}:
                            raise AccessDenied('invalid_input', 422)
                        node_key, protocol = grant['node_key'], grant['protocol']
                        if not isinstance(node_key, str) or not isinstance(protocol, str) or protocol not in {'awg', 'xray'} or (node_key, protocol) in seen:
                            raise AccessDenied('invalid_input', 422)
                        seen.add((node_key, protocol))
                        # Serialize target validation with node draining and
                        # cleanup even under PostgreSQL READ COMMITTED.
                        node = conn.execute('''UPDATE backend_nodes SET enabled = enabled
                            WHERE key = ? RETURNING enabled, protocols_json''', (node_key,)).fetchone()
                        if node is None or not node['enabled'] or protocol not in json.loads(node['protocols_json']):
                            raise AccessDenied('grant_target_unavailable', 422)
                    cursor = conn.execute('UPDATE backend_profiles SET desired_revision = desired_revision + 1 WHERE id = ? AND desired_revision = ? RETURNING id', (profile_id, revision))
                if cursor.fetchone() is None:
                    raise AccessDenied('revision_conflict', 412)
                if action == 'grants':
                    conn.execute('DELETE FROM backend_grants WHERE profile_id = ?', (profile_id,))
                    for node_key, protocol in sorted(seen):
                        conn.execute('INSERT INTO backend_grants(profile_id, node_key, protocol) VALUES (?, ?, ?)', (profile_id, node_key, protocol))
            result = self.result(conn, profile_id)
            operation = OperationRepository.record(conn, actor, profile_id, previous_targets)
            result['operation_id'] = operation['id']
            result['runtime_status'] = operation['status']
            conn.execute('''UPDATE backend_profile_commands SET result_json = ?
                WHERE principal_id = ? AND actor_id = ? AND command_key = ?''', (json.dumps(result), *identity))
            return result
