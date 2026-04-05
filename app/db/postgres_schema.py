from __future__ import annotations

from typing import Iterable


BASE_DDL: Iterable[str] = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS profiles (
        name TEXT PRIMARY KEY,
        created_at TEXT,
        updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS profile_access_methods (
        profile_name TEXT NOT NULL,
        access_code TEXT NOT NULL,
        PRIMARY KEY (profile_name, access_code),
        FOREIGN KEY (profile_name) REFERENCES profiles(name) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS xray_profiles (
        profile_name TEXT PRIMARY KEY,
        uuid TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        short_id TEXT,
        default_transport TEXT,
        FOREIGN KEY (profile_name) REFERENCES profiles(name) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS xray_transports (
        profile_name TEXT NOT NULL,
        transport TEXT NOT NULL,
        PRIMARY KEY (profile_name, transport),
        FOREIGN KEY (profile_name) REFERENCES profiles(name) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS telegram_users (
        telegram_user_id BIGINT PRIMARY KEY,
        chat_id BIGINT,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        profile_name TEXT,
        locale TEXT NOT NULL DEFAULT 'ru',
        access_granted INTEGER NOT NULL DEFAULT 0,
        access_request_pending INTEGER NOT NULL DEFAULT 0,
        access_request_sent_at TEXT,
        notify_access_requests INTEGER NOT NULL DEFAULT 1,
        announcement_silent INTEGER NOT NULL DEFAULT 0,
        telemetry_enabled INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT,
        last_key_at TEXT,
        key_issued_count INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_profile_access_methods_profile
    ON profile_access_methods(profile_name)
    """,
    """
    CREATE TABLE IF NOT EXISTS profile_state (
        profile_name TEXT PRIMARY KEY,
        access_type TEXT NOT NULL DEFAULT 'none',
        created_at TEXT,
        expires_at TEXT,
        frozen INTEGER NOT NULL DEFAULT 0,
        warned_before_exp INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY (profile_name) REFERENCES profiles(name) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS servers (
        key TEXT PRIMARY KEY,
        region TEXT NOT NULL,
        title TEXT NOT NULL,
        flag TEXT NOT NULL,
        transport TEXT NOT NULL,
        public_host TEXT,
        protocol_kinds TEXT NOT NULL DEFAULT '',
        enabled INTEGER NOT NULL DEFAULT 1,
        ssh_host TEXT,
        ssh_port INTEGER NOT NULL DEFAULT 22,
        ssh_user TEXT,
        ssh_key_path TEXT,
        bootstrap_state TEXT NOT NULL DEFAULT 'new',
        notes TEXT,
        xray_config_path TEXT,
        xray_service_name TEXT,
        xray_host TEXT,
        xray_sni TEXT,
        xray_pbk TEXT,
        xray_sid TEXT,
        xray_short_id TEXT,
        xray_fp TEXT,
        xray_flow TEXT,
        xray_tcp_port INTEGER,
        xray_xhttp_port INTEGER,
        xray_xhttp_path_prefix TEXT,
        awg_config_path TEXT,
        awg_iface TEXT,
        awg_public_host TEXT,
        awg_port INTEGER,
        awg_i1_preset TEXT NOT NULL DEFAULT 'quic',
        created_at TEXT,
        updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS awg_server_configs (
        profile_name TEXT NOT NULL,
        server_key TEXT NOT NULL,
        config_text TEXT NOT NULL,
        wg_conf TEXT,
        created_at TEXT,
        PRIMARY KEY (profile_name, server_key),
        FOREIGN KEY (profile_name) REFERENCES profiles(name) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_awg_server_configs_profile
    ON awg_server_configs(profile_name)
    """,
    """
    CREATE TABLE IF NOT EXISTS profile_server_state (
        profile_name TEXT NOT NULL,
        server_key TEXT NOT NULL,
        protocol_kind TEXT NOT NULL,
        desired_enabled INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'pending',
        remote_id TEXT,
        last_error TEXT,
        created_at TEXT,
        updated_at TEXT,
        PRIMARY KEY (profile_name, server_key, protocol_kind),
        FOREIGN KEY (profile_name) REFERENCES profiles(name) ON DELETE CASCADE,
        FOREIGN KEY (server_key) REFERENCES servers(key) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_profile_server_state_profile
    ON profile_server_state(profile_name)
    """,
    """
    CREATE TABLE IF NOT EXISTS traffic_samples (
        profile_name TEXT NOT NULL,
        server_key TEXT NOT NULL,
        protocol_kind TEXT NOT NULL,
        remote_id TEXT NOT NULL,
        rx_bytes_total BIGINT NOT NULL DEFAULT 0,
        tx_bytes_total BIGINT NOT NULL DEFAULT 0,
        sampled_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_traffic_samples_profile_month
    ON traffic_samples(profile_name, protocol_kind, sampled_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_traffic_samples_server_remote
    ON traffic_samples(server_key, protocol_kind, remote_id, sampled_at)
    """,
)


def ensure_schema(conn) -> None:
    for ddl in BASE_DDL:
        conn.execute(ddl)
    conn.execute(
        """
        INSERT INTO schema_meta(key, value)
        VALUES ('schema_version', '5')
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """
    )
    conn.execute(
        """
        INSERT INTO schema_meta(key, value)
        VALUES ('telemetry_enabled_global', '0')
        ON CONFLICT(key) DO NOTHING
        """
    )
