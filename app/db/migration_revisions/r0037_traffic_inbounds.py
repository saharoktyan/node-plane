"""Traffic per inbound of an engine.

Xray counts every inbound of a client separately (its email is NAME.TAG on
each); the totals stay as they were and these columns split them by inbound
(`vless-tcp`, `vless-xhttp`), as JSON `{inbound: [uplink, downlink]}`. AWG
has one inbound and leaves them empty.
"""

COLUMNS = (
    # The agent's last counters per inbound, as `last_uplink`/`last_downlink`.
    ('backend_traffic_peers', 'last_inbounds_json'),
    # This month's bytes per inbound, as `uplink_bytes`/`downlink_bytes`.
    ('backend_traffic_usage', 'inbounds_json'),
    ('backend_node_traffic', 'inbounds_json'),
)


def add_columns(conn):
    for table, column in COLUMNS:
        if getattr(conn, 'backend_name', '') == 'postgres':
            conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} TEXT NOT NULL DEFAULT '{{}}'")
            continue
        existing = {row['name'] for row in conn.execute(f'PRAGMA table_info({table})').fetchall()}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT '{{}}'")


def upgrade(conn):
    add_columns(conn)
