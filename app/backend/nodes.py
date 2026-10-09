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
                       'xray_xhttp_port', 'xray_xhttp_path', 'awg_public_host', 'awg_port',
                       'xray_fingerprint', 'awg_interface', 'awg_i1_preset', 'awg_port_mode'})
_PORTS = frozenset({'xray_tcp_port', 'xray_xhttp_port', 'awg_port'})
_SSH_TARGET = re.compile(r'(?:[A-Za-z_][A-Za-z0-9._-]*@)?(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])\Z')


def protocol_defaults(settings, protocols):
    """Fill omitted public settings; secret protocol material is generated on-node."""
    result = dict(settings)
    host = result.get('public_host')
    defaults = {}
    if 'xray' in protocols:
        defaults.update(xray_sni='www.cloudflare.com', xray_fingerprint='chrome',
                        xray_tcp_port=443, xray_xhttp_port=8443,
                        xray_xhttp_path='/assets')
        if host:
            defaults['xray_host'] = host
    if 'awg' in protocols:
        from .awg_ports import preferred_port
        defaults.update(awg_interface='wg0', awg_i1_preset='quic',
            awg_port_mode='manual' if 'awg_port' in result else 'auto')
        if 'awg_port' not in result:
            defaults['awg_port'] = preferred_port(result.get('awg_i1_preset', 'quic'))
        if host:
            defaults['awg_public_host'] = host
    for field, value in defaults.items():
        result.setdefault(field, value)
    return dict(sorted(result.items()))


def _command_key(value):
    try:
        return str(UUID(value))
    except (TypeError, ValueError, AttributeError):
        raise AccessDenied('invalid_idempotency_key', 422) from None


def _validate(values, *, create):
    allowed = {'key', 'title', 'region', 'flag', 'protocols', 'xray_transports', 'settings',
               'transport', 'ssh_target', 'notes', 'template'} if create else {
        'title', 'region', 'flag', 'protocols', 'xray_transports', 'settings', 'transport', 'ssh_target', 'notes', 'confirm_access_change'}
    if not values or set(values) - allowed:
        raise AccessDenied('invalid_input', 422)
    if create and (set(values) < {'key', 'title', 'region', 'protocols'}):
        raise AccessDenied('invalid_input', 422)
    if 'template' in values:
        from .node_templates import NODE_TEMPLATES
        if values['template'] not in {t.code for t in NODE_TEMPLATES}:
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
    if 'notes' in values and (not isinstance(values['notes'], str) or len(values['notes']) > 2000):
        raise AccessDenied('invalid_input', 422)
    if 'confirm_access_change' in values and type(values['confirm_access_change']) is not bool:
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
            if field == 'awg_interface' and not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,14}', value):
                raise AccessDenied('invalid_input', 422)
            if field == 'awg_i1_preset' and value not in {'quic', 'dns', 'chaos'}:
                raise AccessDenied('invalid_input', 422)
            if field == 'awg_port_mode' and value not in {'auto', 'manual'}:
                raise AccessDenied('invalid_input', 422)
            if field == 'xray_fingerprint' and value not in {'chrome', 'firefox', 'safari', 'ios', 'android', 'edge', 'random', 'randomized'}:
                raise AccessDenied('invalid_input', 422)
        values['settings'] = dict(sorted(settings.items()))
    if create and 'xray' not in values['protocols'] and values.get('xray_transports'):
        raise AccessDenied('invalid_input', 422)
    if 'transport' in values and (not isinstance(values['transport'], str)
                                  or values['transport'] not in {'local', 'ssh'}):
        raise AccessDenied('invalid_input', 422)
    if 'ssh_target' in values and values['ssh_target'] is not None and (
            not isinstance(values['ssh_target'], str)
            or len(values['ssh_target']) > 255
            or not _SSH_TARGET.fullmatch(values['ssh_target'])):
        raise AccessDenied('invalid_input', 422)
    if create and (values.get('transport') == 'ssh') != bool(values.get('ssh_target')):
        raise AccessDenied('invalid_input', 422)
    return values


class NodeService:
    def __init__(self, db):
        self.db = db

    def initialize_schema(self):
        from .backups import BackupService
        BackupService(self.db).initialize_schema()
        from .updates import UpdateService
        UpdateService(self.db, updater=False).initialize_schema()
        from .node_operations import NodeOperations
        NodeOperations(self.db).initialize_schema()
        from .node_removal import NodeRemovalService
        NodeRemovalService(self.db).initialize_schema()
        with self.db.transaction() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS backend_node_notes (node_key TEXT PRIMARY KEY REFERENCES backend_nodes(key) ON DELETE CASCADE, notes TEXT NOT NULL)')
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_node_connections (
                node_key TEXT PRIMARY KEY REFERENCES backend_nodes(key) ON DELETE CASCADE,
                transport TEXT NOT NULL CHECK(transport IN ('local', 'ssh')),
                ssh_target TEXT
            )''')
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_node_commands (
                actor_account_id TEXT NOT NULL REFERENCES backend_accounts(id),
                command_key TEXT NOT NULL,
                input_json TEXT NOT NULL,
                result_json TEXT,
                PRIMARY KEY(actor_account_id, command_key)
            )''')

    @staticmethod
    def public(row):
        return {'key': row['key'], 'title': row['title'], 'region': row['region'], 'region_id': row['region_id'],
                'flag': row['flag'], 'enabled': bool(row['enabled']),
                'policy_eligible': bool(row['policy_eligible']),
                'protocols': json.loads(row['protocols_json']),
                'xray_transports': json.loads(row['xray_transports_json']),
                'desired_revision': row['desired_revision'], 'applied_revision': row['applied_revision'],
                'settings': protocol_defaults(json.loads(row['settings_json']), json.loads(row['protocols_json'])),
                'transport': row['transport'], 'ssh_target': row['ssh_target'], 'notes': row['notes'] or ''}

    @staticmethod
    def _select():
        return '''SELECT n.*, c.transport, c.ssh_target, m.notes, r.region_id,
            CASE WHEN (n.enabled=1 OR n.applied_revision>0)
                AND NOT EXISTS (SELECT 1 FROM backend_node_drains d WHERE d.node_key=n.key)
                AND NOT EXISTS (SELECT 1 FROM backend_node_retirements t WHERE t.node_key=n.key)
                THEN 1 ELSE 0 END AS policy_eligible FROM backend_nodes n
            LEFT JOIN backend_node_connections c ON c.node_key = n.key
            LEFT JOIN backend_node_notes m ON m.node_key = n.key
            LEFT JOIN backend_node_regions r ON r.node_key = n.key'''

    def list(self, actor, *, limit=25, cursor=None, search=None, order="key"):
        require_permission(actor, 'nodes.manage')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AccessDenied('invalid_input', 422)
        if search is not None and (not isinstance(search, str) or not 1 <= len(search.strip()) <= 128):
            raise AccessDenied('invalid_input', 422)
        if order not in {'key', 'region'}:
            raise AccessDenied('invalid_input', 422)
        term = search.strip().casefold() if search else None
        kind = 'admin_nodes_search:' + term if term else 'admin_nodes'
        if order == 'region':
            kind += ':region'
        after = _cursor(cursor, kind)
        if order == 'region':
            try:
                after = json.loads(after) if after else ['', '', '']
                if not isinstance(after, list) or len(after) != 3 or any(not isinstance(v, str) or len(v) > bound for v, bound in zip(after, (128, 128, 64))):
                    raise ValueError()
            except (ValueError, TypeError):
                raise AccessDenied('invalid_cursor', 422) from None
        columns = 'n.region, n.title, n.key' if order == 'region' else 'n.key'
        condition = ' WHERE (n.region, n.title, n.key) > (?, ?, ?)' if order == 'region' else ' WHERE n.key > ?'
        def parameters(position, count):
            return (*position, count) if order == 'region' else (position, count)
        with self.db.connect() as conn:
            if term:
                matches = []
                current = after
                while len(matches) < limit + 1:
                    rows = conn.execute(self._select() + condition + ' ORDER BY ' + columns + ' LIMIT ?',
                                        parameters(current, 200)).fetchall()
                    if not rows:
                        break
                    for row in rows:
                        current = [row['region'], row['title'], row['key']] if order == 'region' else row['key']
                        if any(term in str(row[field]).casefold() for field in ('key', 'title', 'region')):
                            matches.append(row)
                            if len(matches) >= limit + 1:
                                break
                    if len(rows) < 200:
                        break
                rows = matches
            else:
                rows = conn.execute(self._select() + condition + ' ORDER BY ' + columns + ' LIMIT ?',
                                    parameters(after, limit + 1)).fetchall()
        items = [self.public(row) for row in rows]
        if order == 'region':
            for item in items:
                item['_position'] = json.dumps([item['region'], item['title'], item['key']])
            page = _page(items, limit, kind, '_position')
            for item in page['items']:
                item.pop('_position')
            return page
        return _page(items, limit, kind, 'key')

    def get(self, actor, node_key):
        require_permission(actor, 'nodes.manage')
        with self.db.connect() as conn:
            row = conn.execute(self._select() + ' WHERE n.key = ?', (node_key,)).fetchone()
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
            from .maintenance_gate import admit
            admit(conn)
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
                # A single row lock serializes all creators, across administrator
                # accounts, including the local-slot guard and template numbering.
                conn.execute("INSERT INTO backend_system_settings(key,value) VALUES ('node_creation_lock','1') ON CONFLICT(key) DO NOTHING")
                conn.execute("UPDATE backend_system_settings SET value=value WHERE key='node_creation_lock'")
                if values.get('template'):
                    from .node_templates import NODE_TEMPLATES
                    template = next(t for t in NODE_TEMPLATES if t.code == values['template'])
                    keys = [r['key'] for r in conn.execute('SELECT key FROM backend_nodes').fetchall()]
                    keys += [r['node_key'] for r in conn.execute('SELECT node_key FROM backend_node_retirements').fetchall()]
                    values.update(template.draft(keys))
                node_key = values['key']
                if conn.execute('SELECT 1 FROM backend_node_retirements WHERE node_key = ?', (node_key,)).fetchone():
                    raise AccessDenied('node_key_retired', 409)
                if conn.execute('SELECT 1 FROM backend_nodes WHERE key = ?', (node_key,)).fetchone():
                    raise AccessDenied('node_key_conflict', 409)
                from .installation_defaults import local_available, read_defaults
                if values.get('transport', 'local') == 'local' and not local_available(conn):
                    raise AccessDenied('local_node_exists', 409)
                defaults = read_defaults(conn)
                supplied = values.get('settings', {})
                inherited = dict(defaults['settings'])
                if 'awg_port' in supplied and 'awg_port_mode' not in supplied:
                    inherited['awg_port_mode'] = 'manual'
                if supplied.get('awg_port_mode') == 'auto':
                    inherited.pop('awg_port', None)
                values['settings'] = protocol_defaults({**inherited, **supplied}, values['protocols'])
                values.setdefault('xray_transports', (defaults['xray_transports'] or ['tcp', 'xhttp']) if 'xray' in values['protocols'] else [])
                inserted = conn.execute('''INSERT INTO backend_nodes
                    (key, title, region, flag, enabled, protocols_json, xray_transports_json, settings_json)
                    VALUES (?, ?, ?, ?, 0, ?, ?, ?) ON CONFLICT(key) DO NOTHING RETURNING key''',
                    (node_key, values['title'], values['region'], values.get('flag', ''),
                     json.dumps(values['protocols']), json.dumps(values.get('xray_transports', [])),
                     json.dumps(values.get('settings', {}), sort_keys=True))).fetchone()
                if inserted is None:
                    raise AccessDenied('node_key_conflict', 409)
                if values.get('transport'):
                    conn.execute('''INSERT INTO backend_node_connections(node_key, transport, ssh_target)
                        VALUES (?, ?, ?)''', (node_key, values['transport'], values.get('ssh_target')))
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
                if conn.execute("SELECT 1 FROM backend_node_jobs WHERE node_key = ? AND status IN ('awaiting_executor', 'running', 'blocked')", (node_key,)).fetchone():
                    raise AccessDenied('node_operation_pending', 409)
                connection = conn.execute(self._select() + ' WHERE n.key = ?', (node_key,)).fetchone()
                from .grant_policies import canonical_region, region_preview
                if ('region' in values and canonical_region(values['region']) != canonical_region(connection['region'])
                        and region_preview(conn, node_key, values['region'])['affected_profiles']):
                    require_permission(actor, 'grants.manage')
                    if not values.get('confirm_access_change'):
                        raise AccessDenied('region_policy_review_required', 409)
                next_values = self.public(connection)
                next_values.update(values)
                from .installation_defaults import local_available
                if next_values['transport'] in {None, 'local'} and not local_available(conn, node_key):
                    raise AccessDenied('local_node_exists', 409)
                next_values['settings'] = protocol_defaults(next_values['settings'], next_values['protocols'])
                if 'protocols' in values and 'xray_transports' not in values and 'xray' in values['protocols'] and not next_values['xray_transports']:
                    next_values['xray_transports'] = ['tcp', 'xhttp']
                if (next_values['transport'] == 'ssh') != bool(next_values['ssh_target']):
                    raise AccessDenied('invalid_input', 422)
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
                if 'transport' in values or 'ssh_target' in values:
                    if next_values['transport'] is None:
                        conn.execute('DELETE FROM backend_node_connections WHERE node_key = ?', (node_key,))
                    else:
                        conn.execute('''INSERT INTO backend_node_connections(node_key, transport, ssh_target)
                            VALUES (?, ?, ?) ON CONFLICT(node_key) DO UPDATE SET
                            transport = excluded.transport, ssh_target = excluded.ssh_target''',
                            (node_key, next_values['transport'], next_values['ssh_target']))
            from .grant_policies import assign_region, reconcile
            if action == 'create' or 'region' in values:
                assign_region(conn, node_key, values['region'])
                reconcile(conn, actor)
            row = conn.execute(self._select() + ' WHERE n.key = ?', (node_key,)).fetchone()
            if 'notes' in values:
                conn.execute('INSERT INTO backend_node_notes(node_key, notes) VALUES (?, ?) ON CONFLICT(node_key) DO UPDATE SET notes = excluded.notes', (node_key, values['notes']))
                row = conn.execute(self._select() + ' WHERE n.key = ?', (node_key,)).fetchone()
            result = self.public(row)
            conn.execute('''UPDATE backend_node_commands SET result_json = ?
                WHERE actor_account_id = ? AND command_key = ?''',
                (json.dumps(result, sort_keys=True), actor.account.id, key))
            return result
