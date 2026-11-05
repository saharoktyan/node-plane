"""One record per installed node agent: how it was installed and what it last
reported about itself.

Whether a node has an agent used to be inferred from finished jobs, rollouts
and enrollments each time it was read. The record is written when the agent
is installed; the backfill derives it from that same evidence for nodes that
had their agent before this revision. Runtime state, not backed up; a restore
rebuilds it from the restored nodes.
"""

METHODS = ('local', 'token', 'ssh', 'rollout', 'task')


def create_schema(conn):
    conn.execute(f'''CREATE TABLE IF NOT EXISTS backend_agent_installations (
        node_key TEXT PRIMARY KEY REFERENCES backend_nodes(key) ON DELETE CASCADE,
        method TEXT NOT NULL CHECK(method IN {METHODS!r}),
        installed_at TEXT NOT NULL,
        agent_version TEXT, agent_commit TEXT, host_id TEXT, checked_at TEXT)''')


# The evidence the node list used before this revision.
EVIDENCE = """(n.applied_revision>0 OR EXISTS (
    SELECT 1 FROM backend_agent_rollouts a WHERE a.node_key=n.key AND a.status='succeeded')
    OR EXISTS (SELECT 1 FROM backend_node_settings_tasks s WHERE s.node_key=n.key AND s.status='succeeded')
    OR EXISTS (SELECT 1 FROM backend_node_jobs j WHERE j.node_key=n.key AND j.status='succeeded'
        AND j.action IN ('bootstrap','reinstall_keep','reinstall_clean','cleanup_runtime'))
    OR EXISTS (SELECT 1 FROM backend_agent_enrollments e WHERE e.node_key=n.key AND e.status='enrolled'))"""


def backfill(conn, now=None):
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc).isoformat()
    conn.execute(f"""INSERT INTO backend_agent_installations(node_key, method, installed_at)
        SELECT n.key, CASE
            WHEN EXISTS (SELECT 1 FROM backend_agent_ssh_installs i WHERE i.node_key=n.key AND i.status='done') THEN 'ssh'
            WHEN EXISTS (SELECT 1 FROM backend_agent_enrollments e WHERE e.node_key=n.key AND e.status='enrolled') THEN 'token'
            WHEN EXISTS (SELECT 1 FROM backend_agent_rollouts a WHERE a.node_key=n.key AND a.status='succeeded') THEN 'rollout'
            WHEN EXISTS (SELECT 1 FROM backend_node_connections c WHERE c.node_key=n.key AND c.transport='local') THEN 'local'
            ELSE 'task' END, ?
        FROM backend_nodes n WHERE {EVIDENCE}
        ON CONFLICT(node_key) DO NOTHING""", (now,))


def upgrade(conn):
    create_schema(conn)
    backfill(conn)
