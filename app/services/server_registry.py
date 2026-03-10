from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional, Sequence

from config import SQLITE_DB_PATH, SSH_KEY
from db.schema import ensure_schema
from db.sqlite_db import SQLiteDB
from utils.security import validate_server_field, validate_server_key


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_protocol_kinds(value: str | Sequence[str] | None) -> tuple[str, ...]:
    if value is None:
        return tuple()
    if isinstance(value, str):
        parts = [item.strip().lower() for item in value.split(",")]
    else:
        parts = [str(item).strip().lower() for item in value]
    valid = []
    for item in parts:
        if item in {"xray", "awg"} and item not in valid:
            valid.append(item)
    return tuple(valid)


@dataclass(frozen=True)
class RegisteredServer:
    key: str
    region: str
    title: str
    flag: str
    transport: str
    public_host: str
    protocol_kinds: tuple[str, ...]
    enabled: bool
    ssh_host: Optional[str]
    ssh_port: int
    ssh_user: Optional[str]
    ssh_key_path: Optional[str]
    bootstrap_state: str
    notes: str
    xray_config_path: str
    xray_service_name: str
    xray_host: str
    xray_sni: str
    xray_pbk: str
    xray_sid: str
    xray_short_id: str
    xray_fp: str
    xray_flow: str
    xray_tcp_port: int
    xray_xhttp_port: int
    xray_xhttp_path_prefix: str
    awg_config_path: str
    awg_iface: str
    awg_public_host: str
    awg_port: int
    awg_i1_preset: str
    created_at: Optional[str]
    updated_at: Optional[str]

    @property
    def ssh_target(self) -> Optional[str]:
        if self.transport == "local":
            return None
        host = (self.ssh_host or "").strip()
        if not host:
            return None
        if "@" in host:
            return host
        if self.ssh_user:
            return f"{self.ssh_user}@{host}"
        return host


_db = SQLiteDB(SQLITE_DB_PATH)
_schema_ready = False


def _bootstrap(force: bool = False) -> None:
    global _schema_ready
    if _schema_ready and not force:
        return
    with _db.transaction() as conn:
        ensure_schema(conn)
    _schema_ready = True


def _row_to_server(row) -> RegisteredServer:
    protocol_kinds = _parse_protocol_kinds(row["protocol_kinds"])
    public_host = str(row["public_host"] or row["ssh_host"] or "")
    return RegisteredServer(
        key=str(row["key"]),
        region=str(row["region"]),
        title=str(row["title"]),
        flag=str(row["flag"] or "🏳️"),
        transport=str(row["transport"] or "ssh"),
        public_host=public_host,
        protocol_kinds=protocol_kinds,
        enabled=bool(row["enabled"]),
        ssh_host=row["ssh_host"],
        ssh_port=int(row["ssh_port"] or 22),
        ssh_user=row["ssh_user"],
        ssh_key_path=row["ssh_key_path"] or SSH_KEY or None,
        bootstrap_state=str(row["bootstrap_state"] or "new"),
        notes=str(row["notes"] or ""),
        xray_config_path=str(row["xray_config_path"] or "/opt/node-plane-runtime/xray/config.json"),
        xray_service_name=str(row["xray_service_name"] or "xray"),
        xray_host=str(row["xray_host"] or public_host),
        xray_sni=str(row["xray_sni"] or ""),
        xray_pbk=str(row["xray_pbk"] or ""),
        xray_sid=str(row["xray_sid"] or ""),
        xray_short_id=str(row["xray_short_id"] or row["xray_sid"] or ""),
        xray_fp=str(row["xray_fp"] or "chrome"),
        xray_flow=str(row["xray_flow"] or "xtls-rprx-vision"),
        xray_tcp_port=int(row["xray_tcp_port"] or 443),
        xray_xhttp_port=int(row["xray_xhttp_port"] or 8443),
        xray_xhttp_path_prefix=str(row["xray_xhttp_path_prefix"] or "/assets"),
        awg_config_path=str(row["awg_config_path"] or "/opt/node-plane-runtime/amnezia-awg/data/wg0.conf"),
        awg_iface=str(row["awg_iface"] or "wg0"),
        awg_public_host=str(row["awg_public_host"] or public_host),
        awg_port=int(row["awg_port"] or 51820),
        awg_i1_preset=str(row["awg_i1_preset"] or "quic"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def list_servers(include_disabled: bool = False) -> list[RegisteredServer]:
    _bootstrap()
    with _db.connect() as conn:
        sql = "SELECT * FROM servers"
        params: tuple[object, ...] = tuple()
        if not include_disabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY title, key"
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_server(row) for row in rows]


def get_server(server_key: str) -> Optional[RegisteredServer]:
    _bootstrap()
    with _db.connect() as conn:
        row = conn.execute("SELECT * FROM servers WHERE key = ?", (server_key,)).fetchone()
    return _row_to_server(row) if row else None


def upsert_server(
    *,
    key: str,
    region: str,
    title: str,
    flag: str,
    transport: str,
    protocol_kinds: Sequence[str] | str,
    public_host: str = "",
    ssh_host: Optional[str] = None,
    ssh_port: int = 22,
    ssh_user: Optional[str] = None,
    ssh_key_path: Optional[str] = None,
    enabled: bool = True,
    bootstrap_state: str = "new",
    notes: str = "",
) -> RegisteredServer:
    _bootstrap()
    key = validate_server_key(key)
    region = str(validate_server_field("region", region))
    title = str(validate_server_field("title", title))
    flag = str(validate_server_field("flag", flag))
    transport = str(validate_server_field("transport", transport))
    protocol_kinds = validate_server_field("protocol_kinds", protocol_kinds)
    public_host = str(validate_server_field("public_host", public_host or ssh_host or ""))
    ssh_host = str(validate_server_field("ssh_host", ssh_host or "")) or None
    ssh_port = int(validate_server_field("ssh_port", ssh_port))
    ssh_user = str(validate_server_field("ssh_user", ssh_user or "")) or None
    ssh_key_path = str(validate_server_field("ssh_key_path", ssh_key_path or SSH_KEY or "")) if (ssh_key_path or SSH_KEY) else None
    bootstrap_state = str(validate_server_field("bootstrap_state", bootstrap_state))
    notes = str(validate_server_field("notes", notes))
    now = _utcnow()
    proto_csv = ",".join(_parse_protocol_kinds(protocol_kinds))
    with _db.transaction() as conn:
        existing = conn.execute("SELECT created_at FROM servers WHERE key = ?", (key,)).fetchone()
        conn.execute(
            """
            INSERT INTO servers(
                key, region, title, flag, transport, public_host, protocol_kinds, enabled,
                ssh_host, ssh_port, ssh_user, ssh_key_path, bootstrap_state, notes,
                xray_config_path, xray_service_name, xray_host, xray_sni, xray_pbk, xray_sid,
                xray_short_id, xray_fp, xray_flow, xray_tcp_port, xray_xhttp_port,
                xray_xhttp_path_prefix, awg_config_path, awg_iface, awg_public_host, awg_port,
                awg_i1_preset, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                region=excluded.region,
                title=excluded.title,
                flag=excluded.flag,
                transport=excluded.transport,
                public_host=excluded.public_host,
                protocol_kinds=excluded.protocol_kinds,
                enabled=excluded.enabled,
                ssh_host=excluded.ssh_host,
                ssh_port=excluded.ssh_port,
                ssh_user=excluded.ssh_user,
                ssh_key_path=excluded.ssh_key_path,
                bootstrap_state=excluded.bootstrap_state,
                notes=excluded.notes,
                updated_at=excluded.updated_at
            """,
            (
                key,
                region,
                title,
                flag,
                transport,
                public_host or ssh_host or "",
                proto_csv,
                1 if enabled else 0,
                ssh_host,
                int(ssh_port or 22),
                ssh_user,
                ssh_key_path,
                bootstrap_state,
                notes,
                "/opt/node-plane-runtime/xray/config.json",
                "xray",
                public_host or ssh_host or "",
                "",
                "",
                "",
                "",
                "chrome",
                "xtls-rprx-vision",
                443,
                8443,
                "/assets",
                "/opt/node-plane-runtime/amnezia-awg/data/wg0.conf",
                "wg0",
                public_host or ssh_host or "",
                51820,
                "quic",
                existing["created_at"] if existing else now,
                now,
            ),
        )
    server = get_server(key)
    if not server:
        raise RuntimeError(f"Server {key} was not created")
    return server


def update_server_fields(server_key: str, **fields: object) -> RegisteredServer:
    _bootstrap()
    if not fields:
        server = get_server(server_key)
        if not server:
            raise KeyError(server_key)
        return server
    allowed = {
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
    }
    parts = []
    params: list[object] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        value = validate_server_field(key, value)
        if key == "protocol_kinds":
            value = ",".join(_parse_protocol_kinds(value))  # type: ignore[arg-type]
        if key == "enabled":
            value = 1 if value else 0
        parts.append(f"{key} = ?")
        params.append(value)
    parts.append("updated_at = ?")
    params.append(_utcnow())
    params.append(server_key)
    with _db.transaction() as conn:
        conn.execute(f"UPDATE servers SET {', '.join(parts)} WHERE key = ?", tuple(params))
    server = get_server(server_key)
    if not server:
        raise KeyError(server_key)
    return server
