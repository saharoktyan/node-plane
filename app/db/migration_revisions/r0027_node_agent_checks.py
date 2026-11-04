"""Node removal without SSH: the agent's own check of its host.

The agent reports it when it schedules its removal; the controller retires
the node once that agent stops answering. Runtime state, not backed up.
"""


def upgrade(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_node_agent_checks (
        node_key TEXT PRIMARY KEY REFERENCES backend_nodes(key) ON DELETE CASCADE,
        check_json TEXT NOT NULL,
        scheduled_at TEXT NOT NULL)''')
