"""Roles: what an account may do, assigned per scope.

For now the roles are fixed presets (`owner`, `admin`, `operator`,
`support`); their permissions are defined in code (`authorization.PRESETS`)
and the rows here name them. Every active account has the member's own
permissions without an assignment. An assignment has a scope; only the whole
installation is used today, nodes and service instances come later.
Existing administrators become owners.
"""

PRESETS = ('owner', 'admin', 'operator', 'support')
SCOPES = ('installation', 'node', 'service_instance')


def create_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_roles (
        id TEXT PRIMARY KEY,
        preset INTEGER NOT NULL CHECK(preset IN (0, 1)))''')
    conn.execute(f'''CREATE TABLE IF NOT EXISTS backend_role_assignments (
        account_id TEXT NOT NULL REFERENCES backend_accounts(id) ON DELETE CASCADE,
        role_id TEXT NOT NULL REFERENCES backend_roles(id),
        scope_kind TEXT NOT NULL DEFAULT 'installation' CHECK(scope_kind IN {SCOPES!r}),
        scope_id TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(account_id, role_id, scope_kind, scope_id))''')
    for role in PRESETS:
        conn.execute('INSERT INTO backend_roles(id, preset) VALUES (?, 1) ON CONFLICT(id) DO NOTHING', (role,))


def backfill(conn):
    conn.execute("""INSERT INTO backend_role_assignments(account_id, role_id)
        SELECT a.id, 'owner' FROM backend_accounts a WHERE a.role = 'admin'
        AND NOT EXISTS (SELECT 1 FROM backend_role_assignments r WHERE r.account_id = a.id)""")


def upgrade(conn):
    create_schema(conn)
    backfill(conn)
