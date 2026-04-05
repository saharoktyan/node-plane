from __future__ import annotations

import re
import sqlite3
from typing import Iterable

from db.postgres_schema import ensure_schema as ensure_postgres_schema


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
        telegram_user_id INTEGER PRIMARY KEY,
        chat_id INTEGER,
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
)


PROFILE_STATE_DDL = """
CREATE TABLE IF NOT EXISTS profile_state (
    profile_name TEXT PRIMARY KEY,
    access_type TEXT NOT NULL DEFAULT 'none',
    created_at TEXT,
    expires_at TEXT,
    frozen INTEGER NOT NULL DEFAULT 0,
    warned_before_exp INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (profile_name) REFERENCES profiles(name) ON DELETE CASCADE
)
"""


SERVERS_DDL = """
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
"""


AWG_DDL = """
CREATE TABLE IF NOT EXISTS awg_server_configs (
    profile_name TEXT NOT NULL,
    server_key TEXT NOT NULL,
    config_text TEXT NOT NULL,
    wg_conf TEXT,
    created_at TEXT,
    PRIMARY KEY (profile_name, server_key),
    FOREIGN KEY (profile_name) REFERENCES profiles(name) ON DELETE CASCADE
)
"""


_AWG_VPN_RE = re.compile(r"(vpn://[A-Za-z0-9+/=_-]+)")
_AWG_WG_CONF_RE = re.compile(r"(\[Interface\][\s\S]*?\n\[Peer\][\s\S]*?)(?:\n=+|\Z)")


def _extract_awg_vpn_key(text: str) -> str:
    raw = str(text or "")
    if not raw:
        return ""
    m = _AWG_VPN_RE.search(raw)
    return m.group(1) if m else ""


def _extract_awg_wg_conf(text: str) -> str | None:
    raw = str(text or "")
    if not raw:
        return None
    m = _AWG_WG_CONF_RE.search(raw)
    if not m:
        return None
    return m.group(1).strip().replace("\r\n", "\n").replace("\r", "\n")


PROFILE_SERVER_STATE_DDL = """
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
"""


TRAFFIC_SAMPLES_DDL = """
CREATE TABLE IF NOT EXISTS traffic_samples (
    profile_name TEXT NOT NULL,
    server_key TEXT NOT NULL,
    protocol_kind TEXT NOT NULL,
    remote_id TEXT NOT NULL,
    rx_bytes_total INTEGER NOT NULL DEFAULT 0,
    tx_bytes_total INTEGER NOT NULL DEFAULT 0,
    sampled_at TEXT NOT NULL
)
"""


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, name: str) -> set[str]:
    if not _table_exists(conn, name):
        return set()
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({name})").fetchall()}


def _create_servers_table(conn: sqlite3.Connection) -> None:
    conn.execute(SERVERS_DDL)


def _migrate_telegram_users_table(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "telegram_users")
    if columns and "locale" not in columns:
        conn.execute("ALTER TABLE telegram_users ADD COLUMN locale TEXT NOT NULL DEFAULT 'ru'")
    if columns and "profile_name" not in columns:
        conn.execute("ALTER TABLE telegram_users ADD COLUMN profile_name TEXT")
    if columns and "access_granted" not in columns:
        conn.execute("ALTER TABLE telegram_users ADD COLUMN access_granted INTEGER NOT NULL DEFAULT 0")
    if columns and "access_request_pending" not in columns:
        conn.execute("ALTER TABLE telegram_users ADD COLUMN access_request_pending INTEGER NOT NULL DEFAULT 0")
    if columns and "access_request_sent_at" not in columns:
        conn.execute("ALTER TABLE telegram_users ADD COLUMN access_request_sent_at TEXT")
    if columns and "notify_access_requests" not in columns:
        conn.execute("ALTER TABLE telegram_users ADD COLUMN notify_access_requests INTEGER NOT NULL DEFAULT 1")
    if columns and "announcement_silent" not in columns:
        conn.execute("ALTER TABLE telegram_users ADD COLUMN announcement_silent INTEGER NOT NULL DEFAULT 0")
    if columns and "telemetry_enabled" not in columns:
        conn.execute("ALTER TABLE telegram_users ADD COLUMN telemetry_enabled INTEGER NOT NULL DEFAULT 0")


def _migrate_xray_profiles_table(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "xray_profiles")
    if columns and "short_id" not in columns:
        conn.execute("ALTER TABLE xray_profiles ADD COLUMN short_id TEXT")


def _create_profile_state_table(conn: sqlite3.Connection) -> None:
    conn.execute(PROFILE_STATE_DDL)


def _migrate_profile_state_table(conn: sqlite3.Connection) -> None:
    profile_state_columns = _table_columns(conn, "profile_state")
    if not profile_state_columns:
        _create_profile_state_table(conn)
        return

    if "subscription_type" in profile_state_columns and "access_type" not in profile_state_columns:
        conn.execute("ALTER TABLE profile_state RENAME TO profile_state_old")
        _create_profile_state_table(conn)
        conn.execute(
            """
            INSERT INTO profile_state(profile_name, access_type, created_at, expires_at, frozen, warned_before_exp)
            SELECT profile_name, subscription_type, created_at, expires_at, frozen, warned_before_exp
            FROM profile_state_old
            """
        )
        conn.execute("DROP TABLE profile_state_old")
        profile_state_columns = _table_columns(conn, "profile_state")

    if profile_state_columns and "access_type" not in profile_state_columns:
        conn.execute("ALTER TABLE profile_state ADD COLUMN access_type TEXT NOT NULL DEFAULT 'none'")
        conn.execute("UPDATE profile_state SET access_type = 'none' WHERE access_type IS NULL")


def _migrate_servers_table(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "servers")
    if not columns:
        _create_servers_table(conn)
        return

    desired = {
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
    }
    if columns == desired:
        return

    conn.execute("ALTER TABLE servers RENAME TO servers_old")
    _create_servers_table(conn)

    old_rows = conn.execute("SELECT * FROM servers_old").fetchall()
    for row in old_rows:
        row_map = dict(row)
        key = str(row_map.get("key") or "")
        region = str(row_map.get("region") or key)
        protocol_kinds = row_map.get("protocol_kinds")
        if not protocol_kinds:
            if key == "de":
                protocol_kinds = "xray,awg"
            elif key == "lv":
                protocol_kinds = "awg"
            else:
                protocol_kinds = ""
        public_host = row_map.get("public_host") or row_map.get("ssh_host")
        conn.execute(
            """
            INSERT INTO servers(
                key, region, title, flag, transport, public_host, protocol_kinds, enabled,
                ssh_host, ssh_port, ssh_user, ssh_key_path, bootstrap_state, notes,
                xray_config_path, xray_service_name, xray_host, xray_sni, xray_pbk,
                xray_sid, xray_short_id, xray_fp, xray_flow, xray_tcp_port, xray_xhttp_port,
                xray_xhttp_path_prefix, awg_config_path, awg_iface, awg_public_host, awg_port,
                awg_i1_preset, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                region,
                row_map.get("title") or key,
                row_map.get("flag") or "🏳️",
                row_map.get("transport") or "ssh",
                public_host,
                protocol_kinds,
                int(row_map.get("enabled", 1) or 1),
                row_map.get("ssh_host"),
                int(row_map.get("ssh_port") or 22),
                row_map.get("ssh_user"),
                row_map.get("ssh_key_path"),
                row_map.get("bootstrap_state") or "legacy",
                row_map.get("notes"),
                row_map.get("xray_config_path") or "/opt/node-plane-runtime/xray/config.json",
                row_map.get("xray_service_name") or "xray",
                row_map.get("xray_host"),
                row_map.get("xray_sni"),
                row_map.get("xray_pbk"),
                row_map.get("xray_sid"),
                row_map.get("xray_short_id"),
                row_map.get("xray_fp") or "chrome",
                row_map.get("xray_flow") or "xtls-rprx-vision",
                row_map.get("xray_tcp_port") or 443,
                row_map.get("xray_xhttp_port") or 8443,
                row_map.get("xray_xhttp_path_prefix") or "/assets",
                row_map.get("awg_config_path") or "/opt/node-plane-runtime/amnezia-awg/data/wg0.conf",
                row_map.get("awg_iface") or "wg0",
                row_map.get("awg_public_host") or public_host,
                row_map.get("awg_port") or 51820,
                row_map.get("awg_i1_preset") or "quic",
                row_map.get("created_at"),
                row_map.get("updated_at"),
            ),
        )

    conn.execute("DROP TABLE servers_old")


def _create_awg_table(conn: sqlite3.Connection) -> None:
    conn.execute(AWG_DDL)
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_awg_server_configs_profile
        ON awg_server_configs(profile_name)
        """
    )


def _migrate_awg_table(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "awg_server_configs")
    if not columns:
        _create_awg_table(conn)
        return
    if "server_key" in columns:
        rows = conn.execute(
            "SELECT profile_name, server_key, config_text, wg_conf FROM awg_server_configs"
        ).fetchall()
        for row in rows:
            raw_config = str(row["config_text"] or "")
            raw_wg_conf = str(row["wg_conf"] or "")
            sanitized_config = _extract_awg_vpn_key(raw_config)
            sanitized_wg_conf = raw_wg_conf or (_extract_awg_wg_conf(raw_config) or "")
            if sanitized_config != raw_config or sanitized_wg_conf != raw_wg_conf:
                conn.execute(
                    """
                    UPDATE awg_server_configs
                    SET config_text = ?, wg_conf = ?
                    WHERE profile_name = ? AND server_key = ?
                    """,
                    (
                        sanitized_config,
                        sanitized_wg_conf or None,
                        row["profile_name"],
                        row["server_key"],
                    ),
                )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_awg_server_configs_profile
            ON awg_server_configs(profile_name)
            """
        )
        return

    conn.execute("ALTER TABLE awg_server_configs RENAME TO awg_server_configs_old")
    _create_awg_table(conn)
    conn.execute(
        """
        INSERT INTO awg_server_configs(profile_name, server_key, config_text, wg_conf, created_at)
        SELECT profile_name, region, config_text, wg_conf, created_at
        FROM awg_server_configs_old
        """
    )
    conn.execute("DROP TABLE awg_server_configs_old")


def _create_profile_server_state_table(conn: sqlite3.Connection) -> None:
    conn.execute(PROFILE_SERVER_STATE_DDL)
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_profile_server_state_profile
        ON profile_server_state(profile_name)
        """
    )


def _migrate_profile_server_state_table(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "profile_server_state")
    if not columns:
        _create_profile_server_state_table(conn)
        return
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_profile_server_state_profile
        ON profile_server_state(profile_name)
        """
    )


def _create_traffic_samples_table(conn: sqlite3.Connection) -> None:
    conn.execute(TRAFFIC_SAMPLES_DDL)
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_traffic_samples_profile_month
        ON traffic_samples(profile_name, protocol_kind, sampled_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_traffic_samples_server_remote
        ON traffic_samples(server_key, protocol_kind, remote_id, sampled_at)
        """
    )


def _migrate_traffic_samples_table(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "traffic_samples")
    if not columns:
        _create_traffic_samples_table(conn)
        return
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_traffic_samples_profile_month
        ON traffic_samples(profile_name, protocol_kind, sampled_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_traffic_samples_server_remote
        ON traffic_samples(server_key, protocol_kind, remote_id, sampled_at)
        """
    )


def ensure_sqlite_schema(conn: sqlite3.Connection) -> None:
    for ddl in BASE_DDL:
        conn.execute(ddl)
    _migrate_profile_state_table(conn)
    _migrate_telegram_users_table(conn)
    _migrate_xray_profiles_table(conn)
    _migrate_servers_table(conn)
    _migrate_awg_table(conn)
    _migrate_profile_server_state_table(conn)
    _migrate_traffic_samples_table(conn)
    conn.execute(
        """
        INSERT INTO schema_meta(key, value)
        VALUES ('schema_version', '5')
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('telemetry_enabled_global', '0')"
    )


def ensure_schema(conn) -> None:
    if getattr(conn, "backend_name", "") == "postgres":
        ensure_postgres_schema(conn)
        return
    ensure_sqlite_schema(conn)
