"""Role assignments of accounts.

Roles are fixed presets for now (`authorization.PRESETS`); an account holds
them for the whole installation. `backend_accounts.role` mirrors them: an
account with any role is `admin` (staff), one without is `member`.
"""


def assign(conn, account_id, roles):
    """Replace the account's installation-wide roles and update the mirror."""
    from .authorization import STAFF_ROLES, AccessDenied
    roles = set(roles)
    if roles - STAFF_ROLES:
        raise AccessDenied('invalid_input', 422)
    conn.execute("""DELETE FROM backend_role_assignments
        WHERE account_id = ? AND scope_kind = 'installation' AND scope_id = ''""", (account_id,))
    for role in sorted(roles):
        conn.execute('INSERT INTO backend_role_assignments(account_id, role_id) VALUES (?, ?)', (account_id, role))
    conn.execute('UPDATE backend_accounts SET role = ? WHERE id = ?', ('admin' if roles else 'member', account_id))


def owners(conn):
    """Active owners, including administrators of older releases without assignments."""
    return conn.execute("""SELECT COUNT(*) AS count FROM backend_accounts a WHERE a.status = 'approved' AND (
        EXISTS (SELECT 1 FROM backend_role_assignments r WHERE r.account_id = a.id AND r.role_id = 'owner'
            AND r.scope_kind = 'installation')
        OR (a.role = 'admin' AND NOT EXISTS (SELECT 1 FROM backend_role_assignments r WHERE r.account_id = a.id)))
        """).fetchone()['count']


def holds_sql(permission, account='a'):
    """An SQL condition: the account aliased `account` is active staff holding
    `permission` through its installation-wide roles."""
    from .authorization import PRESETS
    roles = ','.join(f"'{role}'" for role, granted in sorted(PRESETS.items()) if permission in granted)
    if not roles:
        return '0=1'
    legacy = ("OR NOT EXISTS (SELECT 1 FROM backend_role_assignments r WHERE r.account_id = "
              f"{account}.id AND r.scope_kind = 'installation')") if permission in PRESETS['owner'] else ''
    return (f"({account}.role = 'admin' AND {account}.status = 'approved' AND ("
            f"EXISTS (SELECT 1 FROM backend_role_assignments r WHERE r.account_id = {account}.id "
            f"AND r.scope_kind = 'installation' AND r.role_id IN ({roles})) {legacy}))")
