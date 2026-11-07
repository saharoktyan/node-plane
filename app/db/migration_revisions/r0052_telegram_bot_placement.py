"""The Telegram bot on a node.

One row while the bot runs on a node instead of the controller: the node,
how far the bot is applied there, the release it runs and its signing key.
Without the row the controller runs the bot itself.
"""


def create_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_telegram_bot_placement (
        id INTEGER PRIMARY KEY CHECK(id = 1),
        node_key TEXT NOT NULL REFERENCES backend_nodes(key),
        status TEXT NOT NULL CHECK(status IN ('applying', 'active', 'failed', 'removing')),
        revision INTEGER NOT NULL,
        applied_revision INTEGER,
        applied_version TEXT,
        key_id TEXT,
        error_code TEXT,
        attempted_at TEXT,
        actor_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL)''')


def upgrade(conn):
    create_schema(conn)
