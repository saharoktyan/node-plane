"""Keep cancelled preflight rollouts terminal without claiming installation success."""
import sqlite3


def upgrade(conn):
    if isinstance(conn, sqlite3.Connection):
        # SQLite is used by contract fixtures; production PostgreSQL retains
        # the table and all its rows while replacing only this constraint.
        conn.execute("""CREATE TABLE backend_agent_rollouts_next (
            id TEXT PRIMARY KEY, node_key TEXT NOT NULL,
            actor_account_id TEXT NOT NULL REFERENCES backend_accounts(id),
            command_key TEXT NOT NULL, intent_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('awaiting_executor','running','blocked','succeeded','cancelled')),
            UNIQUE(actor_account_id,command_key))""")
        conn.execute('INSERT INTO backend_agent_rollouts_next SELECT * FROM backend_agent_rollouts')
        conn.execute('DROP TABLE backend_agent_rollouts')
        conn.execute('ALTER TABLE backend_agent_rollouts_next RENAME TO backend_agent_rollouts')
        conn.execute('CREATE INDEX idx_backend_agent_rollouts_work ON backend_agent_rollouts(status,id)')
    else:
        conn.execute('ALTER TABLE backend_agent_rollouts DROP CONSTRAINT IF EXISTS backend_agent_rollouts_status_check')
        conn.execute("""ALTER TABLE backend_agent_rollouts ADD CONSTRAINT backend_agent_rollouts_status_check
            CHECK(status IN ('awaiting_executor','running','blocked','succeeded','cancelled'))""")
