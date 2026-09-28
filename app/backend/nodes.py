"""Backend-owned node inventory and desired public protocol settings.

Creating or editing a node never claims that its runtime accepted the change.
The applied revision is reserved for a future verified driver reconciliation.
"""
from __future__ import annotations

import json
import re
from uuid import UUID

from .authorization import AccessDenied, require_permission
from .profiles import _cursor, _page


_KEY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z')
_SETTINGS = frozenset({'public_host', 'xray_host', 'xray_sni', 'xray_tcp_port',
                       'xray_xhttp_port', 'xray_xhttp_path', 'awg_public_host', 'awg_port'})
_PORTS = frozenset({'xray_tcp_port', 'xray_xhttp_port', 'awg_port'})


def _command_key(value):
    try:
        return str(UUID(value))
    except (TypeError, ValueError, AttributeError):
        raise AccessDenied('invalid_idempotency_key', 422) from None


def _validate(values, *, create):
    allowed = {'key', 'title', 'region', 'flag', 'protocols', 'xray_transports', 'settings'} if create else {
        'title', 'region', 'flag', 'protocols', 'xray_transports', 'settings'}
    if not values or set(values) - allowed:
        raise AccessDenied('invalid_input', 422)
    if create and (set(values) < {'key', 'title', 'region', 'protocols'}):
        raise AccessDenied('invalid_input', 422)
    if 'key' in values and (not isinstance(values['key'], str) or not _KEY.fullmatch(values['key'])):
        raise AccessDenied('invalid_input', 422)
    for field in ('title', 'region'):
        if field in values and (not isinstance(values[field], str) or not values[field].strip()
                                or len(values[field]) > 128):
            raise AccessDenied('invalid_input', 422)
        if field in values:
            values[field] = values[field].strip()
    if 'flag' in values and (not isinstance(values['flag'], str) or len(values['flag']) > 16):
        raise AccessDenied('invalid_input', 422)
    for field, options in (('protocols', {'awg', 'xray'}), ('xray_transports', {'tcp', 'xhttp'})):
        if field in values:
            selection = values[field]
            if (not isinstance(selection, list) or len(selection) > len(options)
                    or any(not isinstance(item, str) or item not in options for item in selection)
                    or len(set(selection)) != len(selection)):
                raise AccessDenied('invalid_input', 422)
            values[field] = sorted(selection)
    if 'settings' in values:
        settings = values['settings']
        if not isinstance(settings, dict) or set(settings) - _SETTINGS:
            raise AccessDenied('invalid_input', 422)
        for field, value in settings.items():
            if field in _PORTS:
                if type(value) is not int or not 1 <= value <= 65535:
                    raise AccessDenied('invalid_input', 422)
            elif not isinstance(value, str) or not value.strip() or len(value) > 255 or any(ord(c) < 32 for c in value):
                raise AccessDenied('invalid_input', 422)
        values['settings'] = dict(sorted(settings.items()))
    if create and 'xray' not in values['protocols'] and values.get('xray_transports'):
        raise AccessDenied('invalid_input', 422)
    return values


class NodeService:
    def __init__(self, db):
        self.db = db

    def initialize_schema(self):
        with self.db.transaction() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_node_commands (
                actor_account_id TEXT NOT NULL REFERENCES backend_accounts(id),
                command_key TEXT NOT NULL,
                input_json TEXT NOT NULL,
                result_json TEXT,
                PRIMARY KEY(actor_account_id, command_key)
            )''')

    @staticmethod
    def public(row):
        return {'key': row['key'], 'title': row['title'], 'region': row['region'],
                'flag': row['flag'], 'enabled': bool(row['enabled']),
                'protocols': json.loads(row['protocols_json']),
                'xray_transports': json.loads(row['xray_transports_json']),
                'desired_revision': row['desired_revision'], 'applied_revision': row['applied_revision'],
                'settings': json.loads(row['settings_json'])}

    def list(self, actor, *, limit=25, cursor=None):
        require_permission(actor, 'nodes.manage')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AccessDenied('invalid_input', 422)
        after = _cursor(cursor, 'admin_nodes')
        with self.db.connect() as conn:
            rows = conn.execute('SELECT * FROM backend_nodes WHERE key > ? ORDER BY key LIMIT ?',
                                (after, limit + 1)).fetchall()
        return _page([self.public(row) for row in rows], limit, 'admin_nodes', 'key')

    def get(self, actor, node_key):
        require_permission(actor, 'nodes.manage')
        with self.db.connect() as conn:
            row = conn.execute('SELECT * FROM backend_nodes WHERE key = ?', (node_key,)).fetchone()
        if row is None:
            raise AccessDenied('resource_not_found', 404)
        return self.public(row)

    def command(self, actor, *, action, values, command_key, node_key=None, revision=None):
        require_permission(actor, 'nodes.manage')
        key = _command_key(command_key)
        if action not in {'create', 'edit'}:
            raise ValueError('unsupported node command')
        values = _validate(dict(values), create=action == 'create')
        if action == 'edit' and (type(revision) is not int or revision < 1):
            raise AccessDenied('invalid_revision', 422)
        input_json = json.dumps({'action': action, 'node_key': node_key, 'revision': revision,
                                 'values': values}, sort_keys=True, separators=(',', ':'))
        with self.db.transaction() as conn:
            current_actor = conn.execute('''UPDATE backend_accounts SET role = role WHERE id = ?
                RETURNING role, status''', (actor.account.id,)).fetchone()
            if current_actor is None or current_actor['role'] != 'admin' or current_actor['status'] != 'approved':
                raise AccessDenied('permission_denied')
            conn.execute('''INSERT INTO backend_node_commands(actor_account_id, command_key, input_json)
                VALUES (?, ?, ?) ON CONFLICT(actor_account_id, command_key) DO NOTHING''',
                (actor.account.id, key, input_json))
            previous = conn.execute('''SELECT input_json, result_json FROM backend_node_commands
                WHERE actor_account_id = ? AND command_key = ?''', (actor.account.id, key)).fetchone()
            if previous['input_json'] != input_json:
                raise AccessDenied('idempotency_conflict', 409)
            if previous['result_json'] is not None:
                return json.loads(previous['result_json'])
            # Keep a previously retired key fenced forever. Reusing it could
            # deliver an old queued command to an unrelated machine.
            if action == 'create':
                node_key = values['key']
                if conn.execute('SELECT 1 FROM backend_node_retirements WHERE node_key = ?', (node_key,)).fetchone():
                    raise AccessDenied('node_key_retired', 409)
                if conn.execute('SELECT 1 FROM backend_nodes WHERE key = ?', (node_key,)).fetchone():
                    raise AccessDenied('node_key_conflict', 409)
                inserted = conn.execute('''INSERT INTO backend_nodes
                    (key, title, region, flag, enabled, protocols_json, xray_transports_json, settings_json)
                    VALUES (?, ?, ?, ?, 0, ?, ?, ?) ON CONFLICT(key) DO NOTHING RETURNING key''',
                    (node_key, values['title'], values['region'], values.get('flag', ''),
                     json.dumps(values['protocols']), json.dumps(values.get('xray_transports', [])),
                     json.dumps(values.get('settings', {}), sort_keys=True))).fetchone()
                if inserted is None:
                    raise AccessDenied('node_key_conflict', 409)
            else:
                # Row lock also serializes with grant validation and drain.
                row = conn.execute('''UPDATE backend_nodes SET enabled = enabled
                    WHERE key = ? RETURNING *''', (node_key,)).fetchone()
                if row is None:
                    raise AccessDenied('resource_not_found', 404)
                if conn.execute('SELECT 1 FROM backend_node_drains WHERE node_key = ?', (node_key,)).fetchone():
                    raise AccessDenied('node_already_draining', 409)
                if row['desired_revision'] != revision:
                    raise AccessDenied('revision_conflict', 412)
                next_values = self.public(row)
                next_values.update(values)
                if 'xray' not in next_values['protocols'] and next_values['xray_transports']:
                    raise AccessDenied('invalid_input', 422)
                if 'protocols' in values:
                    active_grants = conn.execute('''SELECT DISTINCT protocol FROM backend_grants
                        WHERE node_key = ?''', (node_key,)).fetchall()
                    if any(grant['protocol'] not in next_values['protocols'] for grant in active_grants):
                        raise AccessDenied('node_protocol_in_use', 409)
                conn.execute('''UPDATE backend_nodes SET title = ?, region = ?, flag = ?,
                    protocols_json = ?, xray_transports_json = ?, settings_json = ?,
                    desired_revision = desired_revision + 1
                    WHERE key = ? AND desired_revision = ?''',
                    (next_values['title'], next_values['region'], next_values['flag'],
                     json.dumps(next_values['protocols']), json.dumps(next_values['xray_transports']),
                     json.dumps(next_values['settings'], sort_keys=True), node_key, revision))
                conn.execute("UPDATE backend_node_settings_tasks SET status = 'superseded' WHERE node_key = ? AND status = 'awaiting_executor'", (node_key,))
            row = conn.execute('SELECT * FROM backend_nodes WHERE key = ?', (node_key,)).fetchone()
            result = self.public(row)
            conn.execute('''UPDATE backend_node_commands SET result_json = ?
                WHERE actor_account_id = ? AND command_key = ?''',
                (json.dumps(result, sort_keys=True), actor.account.id, key))
            return result
