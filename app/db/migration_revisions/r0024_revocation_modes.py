"""Temporary access: the revocation mode each node reported.

At issuance it is the mode a revocation would have; after revocation, the
mode it had. Runtime state, not backed up: a row is missing for leases of
earlier controllers, which ignore the table, and after a restore.
"""


def upgrade(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_temporary_revocation_modes (
        config_id TEXT PRIMARY KEY REFERENCES backend_temporary_configs(id) ON DELETE CASCADE,
        mode TEXT NOT NULL CHECK(mode IN ('new_connections_only','connections_closed','peer_removed')))''')
