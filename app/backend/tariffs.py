"""Tariffs: named templates of profile limits, an access term and nodes.

The title and an optional description are texts in several languages
(`backend.localized`); `title` keeps the source language's, which orders
tariffs and must be unique.

Limits stay linked: a tariff's values are a policy layer between the
installation and the profile (backend.vpn_policies.effective), so editing
a tariff changes every profile on it, and a profile's own values still win.
The term and the nodes are copied when a tariff is assigned or renewed:
changing which nodes a profile reaches revokes configs, so it stays an
explicit command per profile, never a side effect of editing a template.
"""
from datetime import datetime, timedelta, timezone
import json
import unicodedata
from uuid import UUID, uuid4

from .authorization import AccessDenied, require_permission

# In the table's order: r0047 added the texts at the end.
TARIFF_COLUMNS = ['id', 'title', 'canonical', 'duration_days', 'access_json', 'policies_json',
                  'revision', 'created_at', 'updated_at', 'titles_json', 'description_json']
ASSIGNMENT_COLUMNS = ['profile_id', 'tariff_id', 'assigned_at']
# What an assignment does with the profile's access term.
EXPIRY = ('keep', 'start', 'extend')


def create_schema(conn):
    from db.migration_revisions.r0022_tariffs import upgrade
    upgrade(conn)
    from db.migration_revisions.r0047_localized_texts import tariff_columns
    tariff_columns(conn)


def policy_keys():
    """Keys a tariff may set: those a profile may set for every node."""
    from .vpn_policies import REGISTRY
    return sorted(name for name, spec in REGISTRY.items()
                  if 'profile' in spec.subjects and 'global' in spec.scopes)


def profile_values(conn, profile_id):
    """The tariff layer of one profile: {key: value}, empty without a tariff."""
    row = conn.execute('''SELECT t.policies_json FROM backend_profile_tariffs a
        JOIN backend_tariffs t ON t.id=a.tariff_id WHERE a.profile_id=?''', (profile_id,)).fetchone()
    return json.loads(row['policies_json']) if row else {}


def canonical(title):
    return ' '.join(unicodedata.normalize('NFKC', title).casefold().split())


def texts(row, language):
    """The title and description a person with this language reads."""
    from . import localized
    titles = localized.loads(row['titles_json'], localized.plain(row['title']))
    description = localized.loads(row['description_json'])
    return {'title': localized.resolve(titles, language),
            'description': localized.resolve(description, language) if description else None}


def public(row, profiles=0, language=None):
    from . import localized
    titles = localized.loads(row['titles_json'], localized.plain(row['title']))
    description = localized.loads(row['description_json'])
    return {'id': row['id'], **texts(row, language), 'titles': localized.public(titles),
            'descriptions': localized.public(description) if description else None,
            'duration_days': row['duration_days'],
            'access_policy': json.loads(row['access_json']) if row['access_json'] else None,
            'policies': json.loads(row['policies_json']), 'revision': row['revision'],
            'profiles': profiles, 'created_at': row['created_at'], 'updated_at': row['updated_at']}


def _policies(values):
    from .vpn_policies import REGISTRY
    if not isinstance(values, dict) or len(values) > 50:
        raise AccessDenied('invalid_input', 422)
    allowed, result = set(policy_keys()), {}
    for key, value in values.items():
        if key not in allowed:
            raise AccessDenied('invalid_input', 422)
        try:
            result[key] = REGISTRY[key].validate(value)
        except (ValueError, TypeError):
            raise AccessDenied('invalid_policy_value', 422) from None
    return dict(sorted(result.items()))


def _fields(conn, values, *, partial):
    from . import localized
    allowed = {'titles', 'descriptions', 'duration_days', 'access_policy', 'policies'}
    if not isinstance(values, dict) or set(values) - allowed or (not partial and 'titles' not in values):
        raise AccessDenied('invalid_input', 422)
    fields = {}
    if 'titles' in values:
        titles = localized.checked(values['titles'], max_length=64, multiline=False)
        title = titles['texts'][titles['source']]
        fields['title'], fields['canonical'], fields['titles_json'] = title, canonical(title), localized.dumps(titles)
    if 'descriptions' in values:
        description = values['descriptions']
        fields['description_json'] = None if description is None else localized.dumps(
            localized.checked(description, max_length=1000))
    if 'duration_days' in values:
        days = values['duration_days']
        if days is not None and (type(days) is not int or not 1 <= days <= 3650):
            raise AccessDenied('invalid_input', 422)
        fields['duration_days'] = days
    if 'access_policy' in values:
        policy = values['access_policy']
        if policy is not None:
            from .grant_policies import validate
            policy = json.dumps(validate(conn, policy), sort_keys=True)
        fields['access_json'] = policy
    if 'policies' in values:
        fields['policies_json'] = json.dumps(_policies(values['policies']), sort_keys=True)
    return fields


def assign(conn, profile_id, owner_admin, current_expiry, values):
    """Link, relink or unlink a profile's tariff inside a profile command.

    Returns the profile's new expiry (unchanged unless asked) and the access
    policy to apply, or None to keep the profile's access."""
    if not isinstance(values, dict) or set(values) - {'tariff_id', 'expiry', 'apply_access'} \
            or 'tariff_id' not in values:
        raise AccessDenied('invalid_input', 422)
    tariff_id, expiry = values['tariff_id'], values.get('expiry', 'keep')
    apply_access = values.get('apply_access', False)
    if expiry not in EXPIRY or type(apply_access) is not bool:
        raise AccessDenied('invalid_input', 422)
    if tariff_id is None:
        if expiry != 'keep' or apply_access:
            raise AccessDenied('invalid_input', 422)
        conn.execute('DELETE FROM backend_profile_tariffs WHERE profile_id=?', (profile_id,))
        return current_expiry, None
    try:
        tariff_id = str(UUID(tariff_id))
    except (TypeError, ValueError, AttributeError):
        raise AccessDenied('invalid_input', 422) from None
    # The lock keeps a concurrent edit or delete from racing the assignment.
    tariff = conn.execute('UPDATE backend_tariffs SET revision=revision WHERE id=? RETURNING *',
                          (tariff_id,)).fetchone()
    if tariff is None:
        raise AccessDenied('tariff_not_found', 404)
    if owner_admin and (expiry != 'keep' or apply_access):
        # Administrators' profiles never expire and keep access to every node.
        raise AccessDenied('admin_access_permanent', 422)
    if apply_access and tariff['access_json'] is None:
        raise AccessDenied('tariff_without_access', 422)
    if expiry != 'keep' and tariff['duration_days'] is None:
        raise AccessDenied('tariff_without_term', 422)
    now = datetime.now(timezone.utc)
    conn.execute('''INSERT INTO backend_profile_tariffs(profile_id,tariff_id,assigned_at) VALUES (?,?,?)
        ON CONFLICT(profile_id) DO UPDATE SET tariff_id=excluded.tariff_id,assigned_at=excluded.assigned_at''',
        (profile_id, tariff_id, now.isoformat()))
    expires_at = current_expiry
    if expiry != 'keep':
        start = now
        if expiry == 'extend' and current_expiry and datetime.fromisoformat(current_expiry) > now:
            # Renewal adds the term after the remaining time.
            start = datetime.fromisoformat(current_expiry)
        expires_at = (start + timedelta(days=tariff['duration_days'])).astimezone(timezone.utc).isoformat()
    return expires_at, json.loads(tariff['access_json']) if apply_access else None


class Tariffs:
    def __init__(self, db):
        self.db = db

    def initialize_schema(self):
        with self.db.transaction() as conn:
            create_schema(conn)

    @staticmethod
    def _counts(conn):
        return {row['tariff_id']: row['count'] for row in conn.execute(
            'SELECT tariff_id,COUNT(*) AS count FROM backend_profile_tariffs GROUP BY tariff_id').fetchall()}

    def list(self, actor):
        require_permission(actor, 'profiles.manage')
        from .localized import actor_language
        with self.db.connect() as conn:
            counts, language = self._counts(conn), actor_language(conn, actor)
            rows = conn.execute('SELECT * FROM backend_tariffs ORDER BY canonical').fetchall()
            return {'items': [public(row, counts.get(row['id'], 0), language) for row in rows], 'keys': policy_keys()}

    def get(self, actor, tariff_id):
        require_permission(actor, 'profiles.manage')
        with self.db.connect() as conn:
            row = conn.execute('SELECT * FROM backend_tariffs WHERE id=?', (str(tariff_id),)).fetchone()
            if row is None:
                raise AccessDenied('resource_not_found', 404)
            from .localized import actor_language
            return public(row, self._counts(conn).get(row['id'], 0), actor_language(conn, actor))

    def create(self, actor, values):
        require_permission(actor, 'settings.manage')
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            fields = _fields(conn, values, partial=False)
            if conn.execute('SELECT 1 FROM backend_tariffs WHERE canonical=?', (fields['canonical'],)).fetchone():
                raise AccessDenied('tariff_exists', 409)
            now = datetime.now(timezone.utc).isoformat()
            row = {'id': str(uuid4()), 'description_json': None, 'duration_days': None, 'access_json': None,
                   'policies_json': '{}',
                   'revision': 1, 'created_at': now, 'updated_at': now, **fields}
            conn.execute(f'''INSERT INTO backend_tariffs ({','.join(TARIFF_COLUMNS)})
                VALUES ({','.join('?' for _ in TARIFF_COLUMNS)})''', tuple(row[c] for c in TARIFF_COLUMNS))
            from .localized import actor_language
            return public(conn.execute('SELECT * FROM backend_tariffs WHERE id=?', (row['id'],)).fetchone(),
                          language=actor_language(conn, actor))

    def edit(self, actor, tariff_id, revision, values):
        require_permission(actor, 'settings.manage')
        if type(revision) is not int or revision < 1:
            raise AccessDenied('revision_required', 428)
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            fields = _fields(conn, values, partial=True)
            if not fields:
                raise AccessDenied('invalid_input', 422)
            current = conn.execute('UPDATE backend_tariffs SET revision=revision WHERE id=? RETURNING revision',
                                   (str(tariff_id),)).fetchone()
            if current is None:
                raise AccessDenied('resource_not_found', 404)
            if current['revision'] != revision:
                raise AccessDenied('revision_conflict', 412)
            if 'canonical' in fields and conn.execute('SELECT 1 FROM backend_tariffs WHERE canonical=? AND id<>?',
                                                      (fields['canonical'], str(tariff_id))).fetchone():
                raise AccessDenied('tariff_exists', 409)
            assignments = ', '.join(f'{field}=?' for field in sorted(fields))
            conn.execute(f'UPDATE backend_tariffs SET {assignments}, revision=revision+1, updated_at=? WHERE id=?',
                         (*(fields[f] for f in sorted(fields)), datetime.now(timezone.utc).isoformat(), str(tariff_id)))
            row = conn.execute('SELECT * FROM backend_tariffs WHERE id=?', (str(tariff_id),)).fetchone()
            from .localized import actor_language
            return public(row, self._counts(conn).get(row['id'], 0), actor_language(conn, actor))

    def delete(self, actor, tariff_id, revision):
        require_permission(actor, 'settings.manage')
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            current = conn.execute('UPDATE backend_tariffs SET revision=revision WHERE id=? RETURNING revision',
                                   (str(tariff_id),)).fetchone()
            if current is None:
                raise AccessDenied('resource_not_found', 404)
            if current['revision'] != revision:
                raise AccessDenied('revision_conflict', 412)
            if conn.execute('SELECT 1 FROM backend_profile_tariffs WHERE tariff_id=? LIMIT 1',
                            (str(tariff_id),)).fetchone():
                # Unlinking would silently change the limits of its profiles.
                raise AccessDenied('tariff_in_use', 409)
            conn.execute('DELETE FROM backend_tariffs WHERE id=?', (str(tariff_id),))
            return {'deleted_tariff_id': str(tariff_id)}

    def of_profile(self, conn, profile_id, language=None):
        """The profile's tariff in the reader's language, or None."""
        row = conn.execute('''SELECT t.*, a.assigned_at FROM backend_profile_tariffs a
            JOIN backend_tariffs t ON t.id=a.tariff_id WHERE a.profile_id=?''', (profile_id,)).fetchone()
        return None if row is None else {'id': row['id'], **texts(row, language),
                                         'duration_days': row['duration_days'], 'assigned_at': row['assigned_at']}
