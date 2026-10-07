"""Portable defaults for future nodes; existing inventory is never rewritten."""
import json

from .authorization import AccessDenied, require_permission

DEFAULTS = {'protocols': ['awg', 'xray'], 'xray_transports': ['tcp', 'xhttp'],
    'settings': {'awg_i1_preset': 'quic', 'awg_port_mode': 'auto'}}
FIELDS = {'awg_i1_preset', 'awg_port_mode', 'awg_port', 'awg_interface',
    'xray_sni', 'xray_fingerprint', 'xray_tcp_port', 'xray_xhttp_port', 'xray_xhttp_path'}


def read_defaults(conn):
    row = conn.execute("SELECT value FROM backend_system_settings WHERE key='installation_defaults'").fetchone()
    return json.loads(row['value']) if row else {'revision': 1, **DEFAULTS}


def local_available(conn, exclude=None):
    return conn.execute('''SELECT 1 FROM backend_nodes n
        LEFT JOIN backend_node_connections c ON c.node_key=n.key
        WHERE COALESCE(c.transport,'local')='local' AND n.key!=? LIMIT 1''',
        (exclude or '',)).fetchone() is None


class InstallationDefaults:
    def __init__(self, db):
        self.db = db

    def get(self, actor):
        require_permission(actor, 'settings.manage')
        with self.db.connect() as conn:
            return read_defaults(conn)

    def creation_options(self, actor):
        require_permission(actor, 'nodes.manage')
        with self.db.connect() as conn:
            return {'local_available': local_available(conn), 'defaults': read_defaults(conn)}

    def update(self, actor, values, revision):
        require_permission(actor, 'settings.manage')
        if type(revision) is not int or revision < 1:
            raise AccessDenied('invalid_revision', 422)
        from .nodes import _validate
        if set(values) != {'protocols', 'xray_transports', 'settings'} or set(values['settings']) - FIELDS:
            raise AccessDenied('invalid_input', 422)
        values = _validate(dict(values), create=False)
        if not values['protocols'] or ('xray' in values['protocols'] and not values['xray_transports']):
            raise AccessDenied('invalid_input', 422)
        if 'xray' not in values['protocols'] and values['xray_transports']:
            raise AccessDenied('invalid_input', 422)
        settings = values['settings']
        mode = settings.get('awg_port_mode', 'auto')
        if mode == 'manual' and 'awg_port' not in settings:
            raise AccessDenied('invalid_input', 422)
        if mode == 'auto' and 'awg_port' in settings:
            raise AccessDenied('invalid_input', 422)
        from .node_settings import _snapshot
        _snapshot({'key': 'defaults-validation', 'desired_revision': 1,
            'protocols_json': json.dumps(values['protocols']),
            'settings_json': json.dumps({'public_host': 'validation.invalid', **settings})})
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            account = conn.execute('SELECT role,status FROM backend_accounts WHERE id=?', (actor.account.id,)).fetchone()
            if account is None or account['role'] != 'admin' or account['status'] != 'approved':
                raise AccessDenied('permission_denied')
            current = read_defaults(conn)
            if current['revision'] != revision:
                if current['revision'] == revision + 1 and {k: current[k] for k in values} == values:
                    return current
                raise AccessDenied('revision_conflict', 412)
            result = {'revision': revision + 1, **values}
            conn.execute("INSERT INTO backend_system_settings(key,value) VALUES ('installation_defaults',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(result, sort_keys=True),))
            return result
