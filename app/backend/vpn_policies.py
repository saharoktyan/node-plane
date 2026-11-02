"""Owned VPN policy values and their effective resolution per runtime target.

Policies describe what is allowed; protocol adapters receive only resolved
values. A feature registers keys with their subjects, scopes and validator
instead of adding columns to nodes or profiles. Device values override profile
values, which override installation values; within one subject a node/protocol
value overrides a node value, which overrides the global value.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Callable
from uuid import UUID, uuid4

from .authorization import AccessDenied, require_permission

SUBJECTS = ('installation', 'profile', 'device')
SCOPES = ('global', 'node', 'node_protocol')
PROTOCOLS = ('awg', 'xray')
MODES = ('off', 'observe', 'enforce')
TABLE_COLUMNS = ['id', 'subject_kind', 'profile_id', 'device_id', 'node_key', 'protocol',
                 'key', 'value_json', 'revision', 'updated_at']


@dataclass(frozen=True)
class PolicyKey:
    name: str
    subjects: frozenset
    scopes: frozenset
    validate: Callable[[Any], Any]
    default: Any = None
    protocols: frozenset = frozenset(PROTOCOLS)
    # Node runtime settings: a change marks affected nodes for settings apply,
    # and node settings intents carry the resolved value as `setting`.
    setting: str | None = None


REGISTRY: dict[str, PolicyKey] = {}


def register(key):
    if key.name in REGISTRY or not key.subjects <= set(SUBJECTS) or not key.scopes <= set(SCOPES):
        raise ValueError('invalid policy key')
    REGISTRY[key.name] = key
    return key


def integer(minimum, maximum):
    def validate(value):
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError()
        return value
    return validate


def choice(*values):
    def validate(value):
        if value not in values:
            raise ValueError()
        return value
    return validate


mode = choice(*MODES)

register(PolicyKey('xray.bittorrent', frozenset({'installation'}), frozenset({'global'}),
                   choice('allow', 'block'), default='allow', protocols=frozenset({'xray'}),
                   setting='xray_bittorrent'))


def runtime_settings(conn, node_key, protocols):
    """Resolved non-default node runtime values for a node settings intent."""
    values = {}
    for spec in REGISTRY.values():
        for protocol in sorted(spec.protocols & set(protocols)) if spec.setting else ():
            value = effective(conn, node_key=node_key, protocol=protocol, keys=[spec.name])[spec.name]
            if value != spec.default:
                values[spec.setting] = value
    return values


def _mark_nodes(conn, spec, node_key):
    # A changed runtime value needs the ordinary per-node settings apply.
    for node in conn.execute('SELECT key,protocols_json FROM backend_nodes').fetchall():
        if (node_key is None or node['key'] == node_key) and spec.protocols & set(json.loads(node['protocols_json'])):
            conn.execute('UPDATE backend_nodes SET desired_revision=desired_revision+1 WHERE key=?', (node['key'],))


def create_schema(conn):
    from db.migration_revisions.r0015_vpn_policies import upgrade
    upgrade(conn)


def _scope(node_key, protocol):
    if protocol is not None:
        return 'node_protocol'
    return 'node' if node_key is not None else 'global'


def _subject_columns(subject_kind, subject_id):
    return {'profile_id': subject_id if subject_kind == 'profile' else None,
            'device_id': subject_id if subject_kind == 'device' else None}


def _rows(conn, subject_kind, subject_id):
    columns = _subject_columns(subject_kind, subject_id)
    return conn.execute('''SELECT * FROM backend_vpn_policies WHERE subject_kind=?
        AND COALESCE(profile_id,'')=? AND COALESCE(device_id,'')=?
        ORDER BY key, COALESCE(node_key,''), COALESCE(protocol,'')''',
        (subject_kind, columns['profile_id'] or '', columns['device_id'] or '')).fetchall()


def public(row):
    return {'key': row['key'], 'node_key': row['node_key'], 'protocol': row['protocol'],
            'value': json.loads(row['value_json']), 'revision': row['revision'],
            'updated_at': row['updated_at']}


def effective(conn, *, profile_id=None, device_id=None, node_key=None, protocol=None, keys=None):
    """Resolve registered keys for one target; unset keys yield their defaults."""
    if protocol is not None and node_key is None:
        raise ValueError('protocol scope requires a node')
    if device_id is not None:
        device = conn.execute('SELECT profile_id FROM backend_devices WHERE id=?', (device_id,)).fetchone()
        if device is None or profile_id not in (None, device['profile_id']):
            raise ValueError('device does not belong to profile')
        profile_id = device['profile_id']
    names = sorted(REGISTRY if keys is None else keys)
    if any(name not in REGISTRY for name in names):
        raise ValueError('unknown policy key')
    subjects = [('installation', None)]
    if profile_id is not None:
        subjects.append(('profile', profile_id))
    if device_id is not None:
        subjects.append(('device', device_id))
    rank = {'global': 0, 'node': 1, 'node_protocol': 2}
    result = {name: REGISTRY[name].default for name in names}
    best = {}
    for subject_rank, (subject_kind, subject_id) in enumerate(subjects):
        for row in _rows(conn, subject_kind, subject_id):
            if row['key'] not in result:
                continue
            if row['node_key'] is not None and row['node_key'] != node_key:
                continue
            if row['protocol'] is not None and row['protocol'] != protocol:
                continue
            order = (subject_rank, rank[_scope(row['node_key'], row['protocol'])])
            if order >= best.get(row['key'], (-1, -1)):
                best[row['key']] = order
                result[row['key']] = json.loads(row['value_json'])
    return result


class VpnPolicies:
    def __init__(self, db):
        self.db = db

    def initialize_schema(self):
        with self.db.transaction() as conn:
            create_schema(conn)

    @staticmethod
    def _authorize(actor, subject_kind):
        if subject_kind not in SUBJECTS:
            raise AccessDenied('invalid_input', 422)
        require_permission(actor, 'settings.manage' if subject_kind == 'installation' else 'profiles.manage')

    @staticmethod
    def _lock_subject(conn, subject_kind, subject_id):
        # Serialize writers per subject; the subject row also proves existence.
        if subject_kind == 'installation':
            if subject_id is not None:
                raise AccessDenied('invalid_input', 422)
            conn.execute("""INSERT INTO backend_system_settings(key,value) VALUES ('vpn_policy_lock','1')
                ON CONFLICT(key) DO NOTHING""")
            conn.execute("UPDATE backend_system_settings SET value=value WHERE key='vpn_policy_lock'")
            return
        if not isinstance(subject_id, str):
            raise AccessDenied('invalid_input', 422)
        if subject_kind == 'profile':
            found = conn.execute('''UPDATE backend_profiles SET desired_revision=desired_revision
                WHERE id=? AND NOT EXISTS (SELECT 1 FROM backend_profile_deletions d WHERE d.profile_id=backend_profiles.id)
                RETURNING id''', (subject_id,)).fetchone()
        else:
            found = conn.execute('''UPDATE backend_devices SET revision=revision
                WHERE id=? AND status='active' RETURNING id''', (subject_id,)).fetchone()
        if found is None:
            raise AccessDenied('resource_not_found', 404)

    @staticmethod
    def _keys(conn, subject_kind, subject_id):
        # Each key the subject may set globally, with its own value and the
        # effective value it currently resolves to.
        own = {row['key']: row for row in _rows(conn, subject_kind, subject_id)
               if row['node_key'] is None and row['protocol'] is None}
        target = {'profile_id': subject_id} if subject_kind == 'profile' else (
            {'device_id': subject_id} if subject_kind == 'device' else {})
        names = sorted(name for name, spec in REGISTRY.items()
                       if subject_kind in spec.subjects and 'global' in spec.scopes)
        resolved = effective(conn, keys=names, **target) if names else {}
        return [{'key': name, 'value': json.loads(own[name]['value_json']) if name in own else None,
                 'revision': own[name]['revision'] if name in own else 0,
                 'default': REGISTRY[name].default, 'effective': resolved[name]} for name in names]

    def get(self, actor, subject_kind, subject_id=None):
        self._authorize(actor, subject_kind)
        with self.db.connect() as conn:
            if subject_kind == 'profile' and not conn.execute(
                    'SELECT 1 FROM backend_profiles WHERE id=?', (subject_id,)).fetchone():
                raise AccessDenied('resource_not_found', 404)
            if subject_kind == 'device' and not conn.execute(
                    'SELECT 1 FROM backend_devices WHERE id=?', (subject_id,)).fetchone():
                raise AccessDenied('resource_not_found', 404)
            return {'subject_kind': subject_kind, 'subject_id': subject_id,
                    'keys': self._keys(conn, subject_kind, subject_id),
                    'values': [public(row) for row in _rows(conn, subject_kind, subject_id)
                               if row['key'] in REGISTRY]}

    @staticmethod
    def _change(conn, subject_kind, change):
        if not isinstance(change, dict) or set(change) - {'key', 'node_key', 'protocol', 'value', 'revision'} \
                or not {'key', 'revision'} <= set(change):
            raise AccessDenied('invalid_input', 422)
        spec = REGISTRY.get(change['key'])
        node_key, protocol = change.get('node_key'), change.get('protocol')
        revision = change['revision']
        if spec is None or subject_kind not in spec.subjects or type(revision) is not int or revision < 0:
            raise AccessDenied('invalid_input', 422)
        if spec.setting and subject_kind != 'installation':
            raise AccessDenied('invalid_input', 422)
        if node_key is not None and not isinstance(node_key, str):
            raise AccessDenied('invalid_input', 422)
        if protocol is not None and (node_key is None or protocol not in spec.protocols):
            raise AccessDenied('invalid_input', 422)
        if _scope(node_key, protocol) not in spec.scopes:
            raise AccessDenied('invalid_input', 422)
        if node_key is not None and not conn.execute('SELECT 1 FROM backend_nodes WHERE key=?', (node_key,)).fetchone():
            raise AccessDenied('resource_not_found', 404)
        value = change.get('value')
        if value is not None:
            try:
                value = spec.validate(value)
            except (ValueError, TypeError):
                raise AccessDenied('invalid_policy_value', 422) from None
        return spec.name, node_key, protocol, value, revision

    def set(self, actor, command_key, subject_kind, subject_id, changes):
        """Apply changes atomically; a None value removes the subject's own value."""
        try:
            command_key = str(UUID(command_key))
        except (TypeError, ValueError, AttributeError):
            raise AccessDenied('invalid_idempotency_key', 422) from None
        self._authorize(actor, subject_kind)
        if not isinstance(changes, list) or not 1 <= len(changes) <= 50:
            raise AccessDenied('invalid_input', 422)
        fingerprint = hashlib.sha256(json.dumps({'subject_kind': subject_kind, 'subject_id': subject_id,
            'changes': changes}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        identity = (actor.principal.id, actor.account.id, command_key)
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            conn.execute('''INSERT INTO backend_vpn_policy_commands(principal_id,actor_id,command_key,fingerprint)
                VALUES (?,?,?,?) ON CONFLICT(principal_id,actor_id,command_key) DO NOTHING''', (*identity, fingerprint))
            record = conn.execute('''SELECT fingerprint,result_json FROM backend_vpn_policy_commands
                WHERE principal_id=? AND actor_id=? AND command_key=?''', identity).fetchone()
            if record['fingerprint'] != fingerprint:
                raise AccessDenied('idempotency_conflict', 409)
            if record['result_json'] is not None:
                return json.loads(record['result_json'])
            self._lock_subject(conn, subject_kind, subject_id)
            columns = _subject_columns(subject_kind, subject_id)
            parsed = [self._change(conn, subject_kind, change) for change in changes]
            if len({item[:3] for item in parsed}) != len(parsed):
                raise AccessDenied('invalid_input', 422)
            timestamp = datetime.now(timezone.utc).isoformat()
            for name, node_key, protocol, value, revision in parsed:
                row = conn.execute('''SELECT id,revision FROM backend_vpn_policies WHERE subject_kind=?
                    AND COALESCE(profile_id,'')=? AND COALESCE(device_id,'')=?
                    AND COALESCE(node_key,'')=? AND COALESCE(protocol,'')=? AND key=?''',
                    (subject_kind, columns['profile_id'] or '', columns['device_id'] or '',
                     node_key or '', protocol or '', name)).fetchone()
                if (row['revision'] if row else 0) != revision:
                    raise AccessDenied('revision_conflict', 412)
                spec = REGISTRY[name]
                if spec.setting:
                    _mark_nodes(conn, spec, node_key)
                if value is None:
                    if row:
                        conn.execute('DELETE FROM backend_vpn_policies WHERE id=?', (row['id'],))
                elif row:
                    conn.execute('''UPDATE backend_vpn_policies SET value_json=?,revision=revision+1,updated_at=?
                        WHERE id=?''', (json.dumps(value, sort_keys=True), timestamp, row['id']))
                else:
                    conn.execute(f'''INSERT INTO backend_vpn_policies ({','.join(TABLE_COLUMNS)})
                        VALUES ({','.join('?' for _ in TABLE_COLUMNS)})''',
                        (str(uuid4()), subject_kind, columns['profile_id'], columns['device_id'],
                         node_key, protocol, name, json.dumps(value, sort_keys=True), 1, timestamp))
            result = {'subject_kind': subject_kind, 'subject_id': subject_id,
                      'keys': self._keys(conn, subject_kind, subject_id),
                      'values': [public(row) for row in _rows(conn, subject_kind, subject_id)
                                 if row['key'] in REGISTRY]}
            conn.execute('''UPDATE backend_vpn_policy_commands SET result_json=?
                WHERE principal_id=? AND actor_id=? AND command_key=?''', (json.dumps(result), *identity))
            return result
