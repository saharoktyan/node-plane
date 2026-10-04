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
        row = conn.execute('''SELECT p.*, CASE WHEN d.profile_id IS NULL THEN 0 ELSE 1 END AS deleting
            FROM backend_profiles p LEFT JOIN backend_profile_deletions d ON d.profile_id = p.id
            WHERE p.id = ?''', (profile_id,)).fetchone()
        grants = conn.execute('SELECT node_key, protocol FROM backend_grants WHERE profile_id = ? ORDER BY node_key, protocol', (profile_id,)).fetchall()
        return {'profile': ProfileRepository.public(row),
                'grants': [{'node_key': r['node_key'], 'protocol': r['protocol']} for r in grants]}

    @staticmethod
    def validate_grants(conn, grants):
        if not isinstance(grants, list) or len(grants) > 100:
            raise AccessDenied('invalid_input', 422)
        seen = set()
        for grant in grants:
            if not isinstance(grant, dict) or set(grant) != {'node_key', 'protocol'}:
                raise AccessDenied('invalid_input', 422)
            node_key, protocol = grant['node_key'], grant['protocol']
            if (not isinstance(node_key, str) or not isinstance(protocol, str)
                    or protocol not in {'awg', 'xray'} or (node_key, protocol) in seen):
                raise AccessDenied('invalid_input', 422)
            seen.add((node_key, protocol))
            # The lock prevents a concurrent drain from accepting a new grant.
            node = conn.execute('''UPDATE backend_nodes SET enabled = enabled
                WHERE key = ? RETURNING enabled, protocols_json''', (node_key,)).fetchone()
            if node is None or not node['enabled'] or protocol not in json.loads(node['protocols_json']):
                raise AccessDenied('grant_target_unavailable', 422)
        return seen

    def execute(self, actor, key, *, action, profile_id=None, revision=None, values=None):
        try:
            key = str(UUID(key))
        except (TypeError, ValueError, AttributeError):
            raise AccessDenied('invalid_idempotency_key', 422) from None
        require_permission(actor, 'grants.manage' if action == 'grants' else 'profiles.manage')
        if action not in {'create', 'edit', 'grants', 'delete'}:
            raise ValueError('unknown profile command')
        values = values or {}
        fingerprint = hashlib.sha256(json.dumps({'action': action, 'profile_id': profile_id,
            'revision': revision, 'values': values}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        identity = (actor.principal.id, actor.account.id, key)
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
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
                if set(values) - {'display_name', 'owner_account_id', 'grants', 'expires_at'}:
                    raise AccessDenied('invalid_input', 422)
                display_name = values.get('display_name')
                if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 128:
                    raise AccessDenied('invalid_input', 422)
                owner = values.get('owner_account_id')
                if owner is not None and conn.execute('UPDATE backend_accounts SET status = status WHERE id = ? RETURNING id', (owner,)).fetchone() is None:
                    raise AccessDenied('owner_not_found', 422)
                grants = self.validate_grants(conn, values.get('grants', []))
                expires_at = self.expiry(conn, owner, values.get('expires_at'))
                profile_id = str(uuid4())
                conn.execute('''INSERT INTO backend_profiles
                    (id, runtime_name, display_name, owner_account_id, created_at, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?)''',
                    (profile_id, 'p_' + uuid4().hex, display_name.strip(), owner,
                     datetime.now(timezone.utc).isoformat(), expires_at))
                for node_key, protocol in sorted(grants):
                    conn.execute('INSERT INTO backend_grants(profile_id, node_key, protocol) VALUES (?, ?, ?)',
                                 (profile_id, node_key, protocol))
            else:
                if revision is None:
                    raise AccessDenied('revision_required', 428)
                row = conn.execute('''SELECT p.desired_revision, p.owner_account_id, d.profile_id AS deleting
                    FROM backend_profiles p LEFT JOIN backend_profile_deletions d ON d.profile_id = p.id
                    WHERE p.id = ?''', (profile_id,)).fetchone()
                if row is None:
                    raise AccessDenied('resource_not_found', 404)
                if row['deleting'] is not None:
                    raise AccessDenied('profile_deleting', 409)
                if type(revision) is not int or revision < 1:
                    raise AccessDenied('invalid_revision', 422)
                if row['desired_revision'] != revision:
                    raise AccessDenied('revision_conflict', 412)
                if action == 'delete':
                    if values:
                        raise AccessDenied('invalid_input', 422)
                    if row['owner_account_id']:
                        conn.execute('UPDATE backend_accounts SET status = status WHERE id = ?',
                                     (row['owner_account_id'],))
                    conn.execute('''INSERT INTO backend_profile_deletions(profile_id, requested_at)
                        VALUES (?, ?)''', (profile_id, datetime.now(timezone.utc).isoformat()))
                    conn.execute('DELETE FROM backend_grants WHERE profile_id = ?', (profile_id,))
                    cursor = conn.execute('''UPDATE backend_profiles
                        SET frozen = 1, desired_revision = desired_revision + 1
                        WHERE id = ? AND desired_revision = ? RETURNING id''', (profile_id, revision))
                elif action == 'edit':
                    if not values or set(values) - {'display_name', 'frozen', 'expires_at', 'grants'}:
                        raise AccessDenied('invalid_input', 422)
                    editing_grants = 'grants' in values
                    if editing_grants:
                        require_permission(actor, 'grants.manage')
                        seen = self.validate_grants(conn, values['grants'])
                        values = {k: v for k, v in values.items() if k != 'grants'}
                    if 'display_name' in values:
                        name = values['display_name']
                        if not isinstance(name, str) or not name.strip() or len(name) > 128:
                            raise AccessDenied('invalid_input', 422)
                        values = {**values, 'display_name': name.strip()}
                    if 'frozen' in values and type(values['frozen']) is not bool:
                        raise AccessDenied('invalid_input', 422)
                    if 'expires_at' in values:
                        values = {**values, 'expires_at': self.expiry(conn, row['owner_account_id'], values['expires_at'])}
                    assignments = ', '.join(f'{field} = ?' for field in sorted(values))
                    assignments = assignments + ', ' if assignments else ''
                    parameters = [int(values[field]) if field == 'frozen' else values[field] for field in sorted(values)]
                    cursor = conn.execute(f'UPDATE backend_profiles SET {assignments}desired_revision = desired_revision + 1 WHERE id = ? AND desired_revision = ? RETURNING id',
                                         (*parameters, profile_id, revision))
                else:
                    grants = values.get('grants')
                    if set(values) != {'grants'}:
                        raise AccessDenied('invalid_input', 422)
                    seen = self.validate_grants(conn, grants)
                    cursor = conn.execute('UPDATE backend_profiles SET desired_revision = desired_revision + 1 WHERE id = ? AND desired_revision = ? RETURNING id', (profile_id, revision))
                if cursor.fetchone() is None:
                    raise AccessDenied('revision_conflict', 412)
                if action == 'delete' and row['owner_account_id']:
                    ProfileRepository.revoke_orphaned_members(conn, row['owner_account_id'])
                if action == 'grants' or (action == 'edit' and editing_grants):
                    conn.execute('DELETE FROM backend_grants WHERE profile_id = ?', (profile_id,))
                    for node_key, protocol in sorted(seen):
                        conn.execute('INSERT INTO backend_grants(profile_id, node_key, protocol) VALUES (?, ?, ?)', (profile_id, node_key, protocol))
            result = self.result(conn, profile_id)
            operation = OperationRepository.record(conn, actor, profile_id, previous_targets)
            if action == 'delete':
                conn.execute('''UPDATE backend_profile_deletions SET operation_id = ?
                    WHERE profile_id = ?''', (operation['id'], profile_id))
            result['operation_id'] = operation['id']
            result['runtime_status'] = operation['status']
            conn.execute('''UPDATE backend_profile_commands SET result_json = ?
                WHERE principal_id = ? AND actor_id = ? AND command_key = ?''', (json.dumps(result), *identity))
            return result

    @staticmethod
    def expiry(conn, owner, value):
        if value is None:
            return None
        account = conn.execute('SELECT role FROM backend_accounts WHERE id = ?', (owner,)).fetchone() if owner else None
        if account and account['role'] == 'admin':
            raise AccessDenied('admin_expiry_forbidden', 422)
        try:
            expires = datetime.fromisoformat(value)
            if expires.tzinfo is None:
                raise ValueError()
            return expires.astimezone(timezone.utc).isoformat()
        except (ValueError, TypeError):
            raise AccessDenied('invalid_input', 422) from None

    @staticmethod
    def restore_admin_expiries(conn, actor=None, account_id=None):
        from .authorization import Account, Actor, Principal, PrincipalKind
        rows = conn.execute('''SELECT p.id, p.owner_account_id FROM backend_profiles p
            JOIN backend_accounts a ON a.id = p.owner_account_id
            WHERE a.role = 'admin' AND p.expires_at IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM backend_profile_deletions d WHERE d.profile_id = p.id)''').fetchall()
        for row in rows:
            if account_id is not None and row['owner_account_id'] != account_id:
                continue
            previous = OperationRepository.targets(conn, row['id'])
            conn.execute('UPDATE backend_profiles SET expires_at = NULL, desired_revision = desired_revision + 1 WHERE id = ?', (row['id'],))
            attributed = actor or Actor(Principal('admin-permanent-access', PrincipalKind.ACCOUNT,
                frozenset(), row['owner_account_id']), Account(row['owner_account_id']))
            OperationRepository.record(conn, attributed, row['id'], previous)
