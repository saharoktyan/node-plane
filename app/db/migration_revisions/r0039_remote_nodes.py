"""A node is local (on the controller's host) or remote (any other host).

The transport was named `ssh` when the controller reached other hosts over
SSH; now it reaches them through their agents, and SSH only installs one.
"""

CONNECTIONS = '''CREATE TABLE IF NOT EXISTS {name} (
    node_key TEXT PRIMARY KEY REFERENCES backend_nodes(key) ON DELETE CASCADE,
    transport TEXT NOT NULL CHECK(transport IN ('local', 'remote')),
    ssh_target TEXT
)'''


def upgrade(conn):
    if getattr(conn, 'backend_name', '') == 'postgres':
        conn.execute('ALTER TABLE backend_node_connections DROP CONSTRAINT IF EXISTS backend_node_connections_transport_check')
        conn.execute("UPDATE backend_node_connections SET transport='remote' WHERE transport='ssh'")
        conn.execute("""ALTER TABLE backend_node_connections ADD CONSTRAINT backend_node_connections_transport_check
            CHECK (transport IN ('local', 'remote'))""")
        return
    # SQLite cannot change a CHECK in place.
    conn.execute(CONNECTIONS.format(name='backend_node_connections_next'))
    conn.execute("""INSERT INTO backend_node_connections_next(node_key, transport, ssh_target)
        SELECT node_key, CASE transport WHEN 'ssh' THEN 'remote' ELSE transport END, ssh_target
        FROM backend_node_connections""")
    conn.execute('DROP TABLE backend_node_connections')
    conn.execute('ALTER TABLE backend_node_connections_next RENAME TO backend_node_connections')
