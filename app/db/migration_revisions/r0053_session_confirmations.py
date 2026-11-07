"""Sessions confirmed again for dangerous actions.

A cabinet session that has just shown its password or passkey once more
gets a row here; dangerous system actions from a cabinet session need one
younger than a few minutes. Workstation and the bot sign their requests
with their own keys and are not asked.
"""


def create_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_session_confirmations (
        session_id TEXT PRIMARY KEY REFERENCES backend_sessions(id) ON DELETE CASCADE,
        method TEXT NOT NULL CHECK(method IN ('password', 'passkey')),
        confirmed_at TEXT NOT NULL)''')


def upgrade(conn):
    create_schema(conn)
