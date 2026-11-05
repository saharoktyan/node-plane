"""Installed node agents: the record of how each one was installed and what it
last reported about itself (version, host)."""
from datetime import datetime, timezone

# Read as part of a node query aliased `n`.
INSTALLED_SQL = 'EXISTS (SELECT 1 FROM backend_agent_installations ai WHERE ai.node_key=n.key)'


EVIDENCE_TABLES = ('backend_agent_ssh_installs', 'backend_agent_enrollments', 'backend_agent_rollouts',
                   'backend_node_settings_tasks', 'backend_node_jobs', 'backend_node_connections')


def _exists(conn, name):
    if getattr(conn, 'backend_name', '') == 'postgres':
        return conn.execute('SELECT to_regclass(?) AS name', (name,)).fetchone()['name'] is not None
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def create_schema(conn):
    from db.migration_revisions.r0032_agent_installations import backfill, create_schema
    create_schema(conn)
    # Older releases running against this schema record no installations;
    # their evidence is picked up on the next start.
    if all(_exists(conn, name) for name in EVIDENCE_TABLES):
        backfill(conn)


def record(conn, node_key, method):
    """Note that the node's agent is installed; the first method stays."""
    conn.execute('''INSERT INTO backend_agent_installations(node_key, method, installed_at)
        VALUES (?, ?, ?) ON CONFLICT(node_key) DO NOTHING''',
        (node_key, method, datetime.now(timezone.utc).isoformat()))


def observe(conn, node_key, facts):
    """Keep what the agent reported about itself in a live inspection."""
    conn.execute('''UPDATE backend_agent_installations SET agent_version=?, agent_commit=?, host_id=?,
        checked_at=? WHERE node_key=?''',
        (facts.get('agent_version') or None, facts.get('agent_commit') or None, facts.get('host_id') or None,
         datetime.now(timezone.utc).isoformat(), node_key))


def public(row):
    if row is None:
        return None
    return {'method': row['method'], 'installed_at': row['installed_at'],
            'version': row['agent_version'], 'commit': row['agent_commit'], 'checked_at': row['checked_at']}


def get(conn, node_key):
    return public(conn.execute('SELECT * FROM backend_agent_installations WHERE node_key=?', (node_key,)).fetchone())
