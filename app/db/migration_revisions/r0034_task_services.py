"""Settings tasks name the service instances they change.

The agent applies a node's configuration as a whole; the task records which
engines (`xray`, `awg`) the change touches, so each instance shows its own
work. NULL for tasks queued before this revision: they touch every engine.
"""


def add_columns(conn):
    if getattr(conn, 'backend_name', '') == 'postgres':
        conn.execute('ALTER TABLE backend_node_settings_tasks ADD COLUMN IF NOT EXISTS services_json TEXT')
        return
    existing = {row['name'] for row in conn.execute('PRAGMA table_info(backend_node_settings_tasks)').fetchall()}
    if 'services_json' not in existing:
        conn.execute('ALTER TABLE backend_node_settings_tasks ADD COLUMN services_json TEXT')


def upgrade(conn):
    add_columns(conn)
