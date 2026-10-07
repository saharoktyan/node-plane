"""Persistent access policies with separate explicit and derived grants."""
import json
import unicodedata
from uuid import uuid4

from .authorization import AccessDenied, require_permission


TABLE_COLUMNS = {
    'backend_regions': ['id', 'title', 'canonical'],
    'backend_node_regions': ['node_key', 'region_id'],
    'backend_grant_policies': ['profile_id', 'explicit_json', 'rules_json', 'exclusions_json'],
}


def canonical_region(title):
    return ' '.join(unicodedata.normalize('NFKC', title).casefold().split())


def assign_region(conn, node_key, title):
    canonical = canonical_region(title)
    conn.execute('''INSERT INTO backend_regions(id,title,canonical) VALUES (?,?,?)
        ON CONFLICT(canonical) DO NOTHING''', (str(uuid4()), title, canonical))
    region = conn.execute('SELECT id FROM backend_regions WHERE canonical=?', (canonical,)).fetchone()
    conn.execute('''INSERT INTO backend_node_regions(node_key,region_id) VALUES (?,?)
        ON CONFLICT(node_key) DO UPDATE SET region_id=excluded.region_id''', (node_key, region['id']))
    return region['id']


def create_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_regions (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, canonical TEXT NOT NULL UNIQUE)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_node_regions (
        node_key TEXT PRIMARY KEY REFERENCES backend_nodes(key) ON DELETE CASCADE,
        region_id TEXT NOT NULL REFERENCES backend_regions(id))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_grant_policies (
        profile_id TEXT PRIMARY KEY REFERENCES backend_profiles(id) ON DELETE CASCADE,
        explicit_json TEXT NOT NULL, rules_json TEXT NOT NULL, exclusions_json TEXT NOT NULL)''')
    for row in conn.execute('''SELECT n.key,n.region FROM backend_nodes n
        WHERE NOT EXISTS (SELECT 1 FROM backend_node_regions r WHERE r.node_key=n.key)''').fetchall():
        assign_region(conn, row['key'], row['region'])


def grant_set(grants):
    return {(g['node_key'], g['protocol']) for g in grants}


def grant_list(grants):
    return [{'node_key': node, 'protocol': protocol} for node, protocol in sorted(grants)]


def read(conn, profile_id):
    row = conn.execute('SELECT * FROM backend_grant_policies WHERE profile_id=?', (profile_id,)).fetchone()
    if row:
        return {'explicit_grants': json.loads(row['explicit_json']),
                'rules': json.loads(row['rules_json']), 'exclusions': json.loads(row['exclusions_json'])}
    grants = conn.execute('SELECT node_key,protocol FROM backend_grants WHERE profile_id=? ORDER BY node_key,protocol', (profile_id,)).fetchall()
    return {'explicit_grants': [dict(g) for g in grants], 'rules': [], 'exclusions': []}


def store(conn, profile_id, values):
    conn.execute('''INSERT INTO backend_grant_policies(profile_id,explicit_json,rules_json,exclusions_json)
        VALUES (?,?,?,?) ON CONFLICT(profile_id) DO UPDATE SET
        explicit_json=excluded.explicit_json,rules_json=excluded.rules_json,exclusions_json=excluded.exclusions_json''',
        (profile_id, json.dumps(values['explicit_grants'], sort_keys=True),
         json.dumps(values['rules'], sort_keys=True), json.dumps(values['exclusions'], sort_keys=True)))


def validate(conn, values):
    if not isinstance(values, dict) or set(values) != {'explicit_grants', 'rules', 'exclusions'}:
        raise AccessDenied('invalid_input', 422)
    from .profile_commands import ProfileCommands
    explicit = ProfileCommands.validate_grants(conn, values['explicit_grants'])
    rules = values['rules']
    if not isinstance(rules, list) or len(rules) > 100:
        raise AccessDenied('invalid_input', 422)
    seen = set()
    normalized = []
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {'scope', 'region_id', 'protocols'}:
            raise AccessDenied('invalid_input', 422)
        scope, region_id, protocols = rule['scope'], rule['region_id'], rule['protocols']
        if not isinstance(scope, str) or scope not in {'all', 'region'} or (scope == 'all' and region_id is not None):
            raise AccessDenied('invalid_input', 422)
        if scope == 'region' and (not isinstance(region_id, str) or not conn.execute(
                'SELECT 1 FROM backend_regions WHERE id=?', (region_id,)).fetchone()):
            raise AccessDenied('region_not_found', 422)
        if (not isinstance(protocols, list) or not protocols or len(protocols) > 2
                or any(p not in ('awg', 'xray') for p in protocols)
                or len(set(protocols)) != len(protocols) or (scope, region_id) in seen):
            raise AccessDenied('invalid_input', 422)
        seen.add((scope, region_id))
        normalized.append({'scope': scope, 'region_id': region_id, 'protocols': sorted(protocols)})
    exclusions = values['exclusions']
    if not isinstance(exclusions, list) or len(exclusions) > 100:
        raise AccessDenied('invalid_input', 422)
    excluded = set()
    for exclusion in exclusions:
        if not isinstance(exclusion, dict) or set(exclusion) != {'node_key', 'protocol'}:
            raise AccessDenied('invalid_input', 422)
        node, protocol = exclusion['node_key'], exclusion['protocol']
        if (not isinstance(node, str) or protocol not in ('awg', 'xray')
                or (node, protocol) in excluded
                or not conn.execute('SELECT 1 FROM backend_nodes WHERE key=?', (node,)).fetchone()):
            raise AccessDenied('invalid_input', 422)
        excluded.add((node, protocol))
    return {'explicit_grants': grant_list(explicit),
            'rules': sorted(normalized, key=lambda r: (r['scope'], r['region_id'] or '')),
            'exclusions': grant_list(excluded)}


def derived(conn, values, region_override=None, preview_node=None):
    result = set()
    for node in conn.execute('''SELECT n.key,n.protocols_json,r.region_id FROM backend_nodes n
        JOIN backend_node_regions r ON r.node_key=n.key
        WHERE (n.enabled=1 OR n.applied_revision>0 OR n.key=?)
        AND NOT EXISTS (SELECT 1 FROM backend_node_drains d WHERE d.node_key=n.key)
        AND NOT EXISTS (SELECT 1 FROM backend_node_retirements t WHERE t.node_key=n.key)''', (preview_node or '',)).fetchall():
        region_id = region_override[1] if region_override and node['key'] == region_override[0] else node['region_id']
        for rule in values['rules']:
            if rule['scope'] == 'all' or rule['region_id'] == region_id:
                result.update((node['key'], p) for p in rule['protocols'] if p in json.loads(node['protocols_json']))
    return result - grant_set(values['exclusions'])


def materialize(conn, profile_id):
    values = read(conn, profile_id)
    effective = grant_set(values['explicit_grants']) | derived(conn, values)
    if conn.execute('SELECT 1 FROM backend_profile_deletions WHERE profile_id=?', (profile_id,)).fetchone():
        effective = set()
    previous = grant_set(conn.execute('SELECT node_key,protocol FROM backend_grants WHERE profile_id=?', (profile_id,)).fetchall())
    if effective != previous:
        conn.execute('DELETE FROM backend_grants WHERE profile_id=?', (profile_id,))
        for node_key, protocol in sorted(effective):
            conn.execute('INSERT INTO backend_grants(profile_id,node_key,protocol) VALUES (?,?,?)', (profile_id, node_key, protocol))
    return effective != previous


def replace_explicit(conn, profile_id, grants):
    values = read(conn, profile_id)
    values['explicit_grants'] = grant_list(grants)
    # A snapshot edit never silently adopts inherited access as manual access.
    store(conn, profile_id, values)
    materialize(conn, profile_id)


def reconcile(conn, actor):
    """Refresh derived grants and enqueue the ordinary revision-fenced outbox."""
    from .operations import OperationRepository
    changed = set()
    for row in conn.execute('SELECT profile_id FROM backend_grant_policies ORDER BY profile_id').fetchall():
        profile_id = row['profile_id']
        previous = OperationRepository.targets(conn, profile_id)
        if materialize(conn, profile_id):
            conn.execute('UPDATE backend_profiles SET desired_revision=desired_revision+1 WHERE id=?', (profile_id,))
            OperationRepository.record(conn, actor, profile_id, previous)
            changed.add(profile_id)
    return changed


def forget_node(conn, node_key):
    for row in conn.execute('SELECT profile_id FROM backend_grant_policies').fetchall():
        values = read(conn, row['profile_id'])
        for field in ('explicit_grants', 'exclusions'):
            values[field] = [g for g in values[field] if g['node_key'] != node_key]
        store(conn, row['profile_id'], values)


def region_preview(conn, node_key, title):
    node = conn.execute('SELECT region FROM backend_nodes WHERE key=?', (node_key,)).fetchone()
    if not node:
        raise AccessDenied('resource_not_found', 404)
    if not isinstance(title, str) or not title.strip() or len(title) > 128:
        raise AccessDenied('invalid_input', 422)
    region = conn.execute('SELECT id FROM backend_regions WHERE canonical=?', (canonical_region(title),)).fetchone()
    region_id = region['id'] if region else None
    profiles = []
    for row in conn.execute('''SELECT g.profile_id FROM backend_grant_policies g
        WHERE NOT EXISTS (SELECT 1 FROM backend_profile_deletions d WHERE d.profile_id=g.profile_id)
        ORDER BY g.profile_id''').fetchall():
        values = read(conn, row['profile_id'])
        explicit = grant_set(values['explicit_grants'])
        # Include future activation of an unbootstrapped node in the review.
        before = explicit | derived(conn, values, preview_node=node_key)
        after = explicit | derived(conn, values, (node_key, region_id), preview_node=node_key)
        if before != after:
            profiles.append({'profile_id': row['profile_id'],
                'added_protocols': sorted(p for n, p in after - before if n == node_key),
                'removed_protocols': sorted(p for n, p in before - after if n == node_key)})
    return {'node_key': node_key, 'region': title.strip(), 'region_id': region_id,
            'affected_profiles': len(profiles), 'profiles': profiles}


class GrantPolicies:
    def __init__(self, db):
        self.db = db

    def regions(self, actor, *, limit=100, cursor=None):
        require_permission(actor, 'grants.manage')
        from .profiles import _cursor, _page
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AccessDenied('invalid_input', 422)
        after = _cursor(cursor, 'grant_regions')
        with self.db.connect() as conn:
            rows = conn.execute('SELECT id,title FROM backend_regions WHERE id>? ORDER BY id LIMIT ?', (after, limit + 1)).fetchall()
        return _page([dict(row) for row in rows], limit, 'grant_regions', 'id')

    def get(self, actor, profile_id):
        require_permission(actor, 'grants.manage')
        with self.db.connect() as conn:
            profile = conn.execute('SELECT desired_revision FROM backend_profiles WHERE id=?', (profile_id,)).fetchone()
            if not profile:
                raise AccessDenied('resource_not_found', 404)
            values = read(conn, profile_id)
            return {'revision': profile['desired_revision'], **values,
                    'inherited_grants': grant_list(derived(conn, values))}

    def preview_region(self, actor, node_key, title):
        require_permission(actor, 'nodes.manage')
        require_permission(actor, 'grants.manage')
        with self.db.connect() as conn:
            return region_preview(conn, node_key, title)
