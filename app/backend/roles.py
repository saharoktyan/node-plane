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
