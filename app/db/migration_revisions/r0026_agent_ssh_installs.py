"""Install over the controller's SSH access: the enrollment's command runs once
on the server over SSH. Runtime state, not backed up; a restore clears it.
"""


def upgrade(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_agent_ssh_installs (
        node_key TEXT PRIMARY KEY REFERENCES backend_nodes(key) ON DELETE CASCADE,
        ssh_target TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'done', 'failed')),
        error_code TEXT, created_at TEXT NOT NULL)''')
