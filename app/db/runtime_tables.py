"""PostgreSQL table inventory for the retained monolithic reference services."""
TABLE_COLUMNS: list[tuple[str, list[str]]] = [
    ("schema_meta", ["key", "value"]),
    ("profiles", ["name", "created_at", "updated_at"]),
    (
        "profile_state",
        ["profile_name", "access_type", "created_at", "expires_at", "frozen", "warned_before_exp"],
    ),
    ("profile_access_methods", ["profile_name", "access_code"]),
    ("xray_profiles", ["profile_name", "uuid", "enabled", "short_id", "default_transport"]),
    ("xray_transports", ["profile_name", "transport"]),
    (
        "telegram_users",
        [
            "telegram_user_id",
            "chat_id",
            "username",
            "first_name",
            "last_name",
            "profile_name",
            "locale",
            "access_granted",
            "access_request_pending",
            "access_request_sent_at",
            "notify_access_requests",
            "announcement_silent",
            "telemetry_enabled",
            "updated_at",
            "last_key_at",
            "key_issued_count",
        ],
    ),
    (
        "servers",
        [
            "key",
            "region",
            "title",
            "flag",
            "transport",
            "public_host",
            "protocol_kinds",
            "enabled",
            "ssh_host",
            "ssh_port",
            "ssh_user",
            "ssh_key_path",
            "bootstrap_state",
            "notes",
            "xray_config_path",
            "xray_service_name",
            "xray_host",
            "xray_sni",
            "xray_pbk",
            "xray_sid",
            "xray_short_id",
            "xray_fp",
            "xray_flow",
            "xray_tcp_port",
            "xray_xhttp_port",
            "xray_xhttp_path_prefix",
            "awg_config_path",
            "awg_iface",
            "awg_public_host",
            "awg_port",
            "awg_i1_preset",
            "created_at",
            "updated_at",
        ],
    ),
    ("awg_server_configs", ["profile_name", "server_key", "config_text", "wg_conf", "created_at"]),
    (
        "profile_server_state",
        [
            "profile_name",
            "server_key",
            "protocol_kind",
            "desired_enabled",
            "status",
            "remote_id",
            "last_error",
            "created_at",
            "updated_at",
        ],
    ),
    (
        "traffic_samples",
        [
            "profile_name",
            "server_key",
            "protocol_kind",
            "remote_id",
            "rx_bytes_total",
            "tx_bytes_total",
            "sampled_at",
        ],
    ),
]

ALERT_STATE_COLUMNS = [
    "alert_key",
    "server_key",
    "alert_type",
    "severity",
    "payload_json",
    "active",
    "hit_streak",
    "clear_streak",
    "first_seen_at",
    "last_seen_at",
    "last_sent_at",
]

ALERT_STATE_DDL = """
CREATE TABLE IF NOT EXISTS alert_state (
    alert_key TEXT PRIMARY KEY,
    server_key TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    active INTEGER NOT NULL DEFAULT 0,
    hit_streak INTEGER NOT NULL DEFAULT 0,
    clear_streak INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL DEFAULT '',
    last_seen_at TEXT NOT NULL DEFAULT '',
    last_sent_at TEXT NOT NULL DEFAULT ''
)
"""


def _generic_table_exists(conn, name):
    row = conn.execute("SELECT to_regclass(?) AS table_name", (name,)).fetchone()
    return row is not None and row["table_name"] is not None
