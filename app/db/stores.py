from __future__ import annotations

import json
from typing import Any, Callable, Dict, List

from db.sqlite_db import SQLiteDB


def _decode_xray_short_id(value: Any) -> tuple[str, Dict[str, str]]:
    raw = str(value or "").strip()
    if not raw:
        return "", {}
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except Exception:
            return raw, {}
        if isinstance(data, dict):
            mapping = {
                str(server_key): str(short_id).strip()
                for server_key, short_id in data.items()
                if str(server_key).strip() and str(short_id).strip()
            }
            return "", mapping
    return raw, {}


def _encode_xray_short_id(xray: Dict[str, Any] | None) -> str | None:
    if not isinstance(xray, dict):
        return None
    mapping = xray.get("server_short_ids")
    if isinstance(mapping, dict):
        normalized = {
            str(server_key): str(short_id).strip()
            for server_key, short_id in mapping.items()
            if str(server_key).strip() and str(short_id).strip()
        }
        if normalized:
            return json.dumps(normalized, ensure_ascii=False, sort_keys=True)
    short_id = str(xray.get("short_id") or "").strip()
    return short_id or None


class SQLiteProfileStateStore:
    def __init__(self, db: SQLiteDB) -> None:
        self.db = db

    def _read_conn(self, conn) -> Dict[str, Any]:
        rows = conn.execute(
            """
            SELECT
                p.name,
                p.created_at AS profile_created_at,
                p.updated_at,
                s.access_type,
                s.created_at AS state_created_at,
                s.expires_at,
                s.frozen,
                s.warned_before_exp,
                x.uuid,
                x.enabled AS xray_enabled,
                x.short_id,
                x.default_transport
            FROM profiles p
            LEFT JOIN profile_state s ON s.profile_name = p.name
            LEFT JOIN xray_profiles x ON x.profile_name = p.name
            ORDER BY p.name
            """
        ).fetchall()

        result: Dict[str, Any] = {}
        for row in rows:
            name = str(row["name"])
            rec: Dict[str, Any] = {
                "type": row["access_type"] or "none",
                "created_at": row["state_created_at"] or row["profile_created_at"],
                "expires_at": row["expires_at"],
                "frozen": bool(row["frozen"]) if row["frozen"] is not None else False,
                "warned_before_exp": bool(row["warned_before_exp"]) if row["warned_before_exp"] is not None else False,
                "updated_at": row["updated_at"],
            }
            if row["uuid"] is not None:
                short_id, server_short_ids = _decode_xray_short_id(row["short_id"])
                transports = [
                    str(item["transport"])
                    for item in conn.execute(
                        "SELECT transport FROM xray_transports WHERE profile_name = ? ORDER BY transport",
                        (name,),
                    ).fetchall()
                ]
                rec["uuid"] = row["uuid"]
                rec["xray"] = {
                    "enabled": bool(row["xray_enabled"]) if row["xray_enabled"] is not None else True,
                    "transports": transports or ["tcp", "xhttp"],
                    "short_id": short_id,
                    "server_short_ids": server_short_ids,
                    "default": row["default_transport"] or "xhttp",
                }
            access_codes = [
                str(item["access_code"])
                for item in conn.execute(
                    "SELECT access_code FROM profile_access_methods WHERE profile_name = ? ORDER BY access_code",
                    (name,),
                ).fetchall()
            ]
            if access_codes:
                rec["protocols"] = access_codes
            result[name] = rec
        return result

    def read(self) -> Dict[str, Any]:
        with self.db.connect() as conn:
            return self._read_conn(conn)

    def _write_conn(self, conn, data: Dict[str, Any]) -> None:
        names: List[str] = [
            str(name)
            for name in sorted(data.keys())
            if isinstance(data.get(name), dict)
        ]
        existing_names = {
            str(row["name"])
            for row in conn.execute("SELECT name FROM profiles").fetchall()
        }
        stale_names = sorted(existing_names - set(names))
        if stale_names:
            placeholders = ",".join("?" for _ in stale_names)
            conn.execute(f"DELETE FROM profiles WHERE name IN ({placeholders})", stale_names)
        if names:
            pass
        for name in names:
            rec = data.get(name)
            created_at = rec.get("created_at")
            updated_at = rec.get("updated_at") or created_at
            conn.execute(
                """
                INSERT INTO profiles(name, created_at, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at
                """
                ,
                (name, created_at, updated_at),
            )
            conn.execute(
                """
                INSERT INTO profile_state(profile_name, access_type, created_at, expires_at, frozen, warned_before_exp)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_name) DO UPDATE SET
                    access_type=excluded.access_type,
                    created_at=excluded.created_at,
                    expires_at=excluded.expires_at,
                    frozen=excluded.frozen,
                    warned_before_exp=excluded.warned_before_exp
                """,
                (
                    name,
                    rec.get("type", "none"),
                    created_at,
                    rec.get("expires_at"),
                    1 if rec.get("frozen") else 0,
                    1 if rec.get("warned_before_exp") else 0,
                ),
            )
            protocols = rec.get("protocols")
            conn.execute("DELETE FROM profile_access_methods WHERE profile_name = ?", (name,))
            if isinstance(protocols, list):
                for code in sorted({str(item) for item in protocols}):
                    conn.execute(
                        "INSERT INTO profile_access_methods(profile_name, access_code) VALUES (?, ?)",
                        (name, code),
                    )
            xray = rec.get("xray")
            uuid_val = rec.get("uuid")
            if uuid_val is not None or isinstance(xray, dict):
                transports = ["xhttp", "tcp"]
                default_transport = "xhttp"
                enabled = True
                short_id = None
                if isinstance(xray, dict):
                    raw_transports = xray.get("transports")
                    if isinstance(raw_transports, list) and raw_transports:
                        transports = [str(item) for item in raw_transports]
                    default_transport = str(xray.get("default") or default_transport)
                    enabled = bool(xray.get("enabled", True))
                    short_id = _encode_xray_short_id(xray)
                conn.execute(
                    """
                    INSERT INTO xray_profiles(profile_name, uuid, enabled, short_id, default_transport)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(profile_name) DO UPDATE SET
                        uuid=excluded.uuid,
                        enabled=excluded.enabled,
                        short_id=excluded.short_id,
                        default_transport=excluded.default_transport
                    """,
                    (name, uuid_val, 1 if enabled else 0, short_id, default_transport),
                )
                conn.execute("DELETE FROM xray_transports WHERE profile_name = ?", (name,))
                for transport in sorted(set(transports)):
                    conn.execute(
                        "INSERT INTO xray_transports(profile_name, transport) VALUES (?, ?)",
                        (name, transport),
                    )
            else:
                conn.execute("DELETE FROM xray_profiles WHERE profile_name = ?", (name,))
                conn.execute("DELETE FROM xray_transports WHERE profile_name = ?", (name,))

    def write(self, data: Dict[str, Any]) -> None:
        with self.db.transaction() as conn:
            self._write_conn(conn, data)

    def update(self, mutator: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
        with self.db.transaction() as conn:
            data = self._read_conn(conn)
            new_data = mutator(data)
            if not isinstance(new_data, dict):
                raise ValueError("SQLiteProfileStateStore.update mutator must return dict")
            self._write_conn(conn, new_data)
            return new_data


class SQLiteTelegramUsersStore:
    def __init__(self, db: SQLiteDB) -> None:
        self.db = db

    def _read_conn(self, conn) -> Dict[str, Any]:
        rows = conn.execute(
            """
            SELECT telegram_user_id, chat_id, username, first_name, last_name, profile_name, locale,
                   access_granted, access_request_pending, access_request_sent_at,
                   notify_access_requests, announcement_silent, telemetry_enabled,
                   updated_at, last_key_at, key_issued_count
            FROM telegram_users
            ORDER BY telegram_user_id
            """
        ).fetchall()
        result: Dict[str, Any] = {}
        for row in rows:
            result[str(row["telegram_user_id"])] = {
                "chat_id": row["chat_id"],
                "username": row["username"] or "",
                "first_name": row["first_name"] or "",
                "last_name": row["last_name"] or "",
                "profile_name": row["profile_name"],
                "locale": row["locale"] or "ru",
                "access_granted": bool(row["access_granted"]) if row["access_granted"] is not None else False,
                "access_request_pending": bool(row["access_request_pending"]) if row["access_request_pending"] is not None else False,
                "access_request_sent_at": row["access_request_sent_at"],
                "notify_access_requests": bool(row["notify_access_requests"]) if row["notify_access_requests"] is not None else True,
                "announcement_silent": bool(row["announcement_silent"]) if row["announcement_silent"] is not None else False,
                "telemetry_enabled": bool(row["telemetry_enabled"]) if row["telemetry_enabled"] is not None else False,
                "updated_at": row["updated_at"],
                "last_key_at": row["last_key_at"],
                "key_issued_count": int(row["key_issued_count"] or 0),
            }
        return result

    def read(self) -> Dict[str, Any]:
        with self.db.connect() as conn:
            return self._read_conn(conn)

    def _write_conn(self, conn, data: Dict[str, Any]) -> None:
        existing_ids = {
            str(row["telegram_user_id"])
            for row in conn.execute("SELECT telegram_user_id FROM telegram_users").fetchall()
        }
        target_ids = {
            str(raw_user_id)
            for raw_user_id, rec in data.items()
            if isinstance(rec, dict) and str(raw_user_id).isdigit()
        }
        stale_ids = sorted(existing_ids - target_ids, key=int)
        if stale_ids:
            placeholders = ",".join("?" for _ in stale_ids)
            conn.execute(f"DELETE FROM telegram_users WHERE telegram_user_id IN ({placeholders})", tuple(int(item) for item in stale_ids))
        for raw_user_id in sorted(data.keys(), key=lambda value: int(value) if str(value).isdigit() else str(value)):
            rec = data.get(raw_user_id)
            if not isinstance(rec, dict):
                continue
            try:
                telegram_user_id = int(raw_user_id)
            except (TypeError, ValueError):
                continue
            conn.execute(
                """
                INSERT INTO telegram_users(
                    telegram_user_id, chat_id, username, first_name, last_name, profile_name,
                    locale, access_granted, access_request_pending, access_request_sent_at,
                    notify_access_requests, announcement_silent, telemetry_enabled,
                    updated_at, last_key_at, key_issued_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    chat_id=excluded.chat_id,
                    username=excluded.username,
                    first_name=excluded.first_name,
                    last_name=excluded.last_name,
                    profile_name=excluded.profile_name,
                    locale=excluded.locale,
                    access_granted=excluded.access_granted,
                    access_request_pending=excluded.access_request_pending,
                    access_request_sent_at=excluded.access_request_sent_at,
                    notify_access_requests=excluded.notify_access_requests,
                    announcement_silent=excluded.announcement_silent,
                    telemetry_enabled=excluded.telemetry_enabled,
                    updated_at=excluded.updated_at,
                    last_key_at=excluded.last_key_at,
                    key_issued_count=excluded.key_issued_count
                """
                ,
                (
                    telegram_user_id,
                    rec.get("chat_id"),
                    rec.get("username"),
                    rec.get("first_name"),
                    rec.get("last_name"),
                    rec.get("profile_name"),
                    rec.get("locale") or "ru",
                    1 if rec.get("access_granted") else 0,
                    1 if rec.get("access_request_pending") else 0,
                    rec.get("access_request_sent_at"),
                    1 if rec.get("notify_access_requests", True) else 0,
                    1 if rec.get("announcement_silent") else 0,
                    1 if rec.get("telemetry_enabled") else 0,
                    rec.get("updated_at"),
                    rec.get("last_key_at"),
                    int(rec.get("key_issued_count") or 0),
                ),
            )

    def write(self, data: Dict[str, Any]) -> None:
        with self.db.transaction() as conn:
            self._write_conn(conn, data)

    def update(self, mutator: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
        with self.db.transaction() as conn:
            data = self._read_conn(conn)
            new_data = mutator(data)
            if not isinstance(new_data, dict):
                raise ValueError("SQLiteTelegramUsersStore.update mutator must return dict")
            self._write_conn(conn, new_data)
            return new_data

    def upsert_user(self, telegram_user_id: int, **fields: Any) -> Dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute(
                """
                SELECT chat_id, username, first_name, last_name, profile_name, locale,
                       access_granted, access_request_pending, access_request_sent_at,
                       notify_access_requests, announcement_silent, telemetry_enabled,
                       updated_at, last_key_at, key_issued_count
                FROM telegram_users
                WHERE telegram_user_id = ?
                """,
                (int(telegram_user_id),),
            ).fetchone()
            rec: Dict[str, Any] = {
                "chat_id": row["chat_id"] if row else None,
                "username": (row["username"] or "") if row else "",
                "first_name": (row["first_name"] or "") if row else "",
                "last_name": (row["last_name"] or "") if row else "",
                "profile_name": row["profile_name"] if row else None,
                "locale": (row["locale"] or "ru") if row else "ru",
                "access_granted": bool(row["access_granted"]) if row and row["access_granted"] is not None else False,
                "access_request_pending": bool(row["access_request_pending"]) if row and row["access_request_pending"] is not None else False,
                "access_request_sent_at": row["access_request_sent_at"] if row else None,
                "notify_access_requests": bool(row["notify_access_requests"]) if row and row["notify_access_requests"] is not None else True,
                "announcement_silent": bool(row["announcement_silent"]) if row and row["announcement_silent"] is not None else False,
                "telemetry_enabled": bool(row["telemetry_enabled"]) if row and row["telemetry_enabled"] is not None else False,
                "updated_at": row["updated_at"] if row else None,
                "last_key_at": row["last_key_at"] if row else None,
                "key_issued_count": int(row["key_issued_count"] or 0) if row else 0,
            }
            rec.update(fields)
            conn.execute(
                """
                INSERT INTO telegram_users(
                    telegram_user_id, chat_id, username, first_name, last_name, profile_name,
                    locale, access_granted, access_request_pending, access_request_sent_at,
                    notify_access_requests, announcement_silent, telemetry_enabled,
                    updated_at, last_key_at, key_issued_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    chat_id=excluded.chat_id,
                    username=excluded.username,
                    first_name=excluded.first_name,
                    last_name=excluded.last_name,
                    profile_name=excluded.profile_name,
                    locale=excluded.locale,
                    access_granted=excluded.access_granted,
                    access_request_pending=excluded.access_request_pending,
                    access_request_sent_at=excluded.access_request_sent_at,
                    notify_access_requests=excluded.notify_access_requests,
                    announcement_silent=excluded.announcement_silent,
                    telemetry_enabled=excluded.telemetry_enabled,
                    updated_at=excluded.updated_at,
                    last_key_at=excluded.last_key_at,
                    key_issued_count=excluded.key_issued_count
                """,
                (
                    int(telegram_user_id),
                    rec.get("chat_id"),
                    rec.get("username"),
                    rec.get("first_name"),
                    rec.get("last_name"),
                    rec.get("profile_name"),
                    rec.get("locale") or "ru",
                    1 if rec.get("access_granted") else 0,
                    1 if rec.get("access_request_pending") else 0,
                    rec.get("access_request_sent_at"),
                    1 if rec.get("notify_access_requests", True) else 0,
                    1 if rec.get("announcement_silent") else 0,
                    1 if rec.get("telemetry_enabled") else 0,
                    rec.get("updated_at"),
                    rec.get("last_key_at"),
                    int(rec.get("key_issued_count") or 0),
                ),
            )
            return rec

    def bump_key_stat(self, telegram_user_id: int, at: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO telegram_users(telegram_user_id, locale, last_key_at, key_issued_count)
                VALUES (?, 'ru', ?, 1)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    last_key_at=excluded.last_key_at,
                    key_issued_count=telegram_users.key_issued_count + 1
                """,
                (int(telegram_user_id), at),
            )


class SQLiteAWGStore:
    def __init__(self, db: SQLiteDB) -> None:
        self.db = db

    def _read_conn(self, conn) -> Dict[str, Any]:
        rows = conn.execute(
            """
            SELECT profile_name, server_key, config_text, wg_conf, created_at
            FROM awg_server_configs
            ORDER BY profile_name, server_key
            """
        ).fetchall()
        result: Dict[str, Any] = {}
        for row in rows:
            profile_name = str(row["profile_name"])
            profile = result.setdefault(profile_name, {"servers": {}})
            profile["servers"][str(row["server_key"])] = {
                "server_key": row["server_key"],
                "config": row["config_text"] or "",
                "wg_conf": row["wg_conf"],
                "created_at": row["created_at"],
            }
        return result

    def read(self) -> Dict[str, Any]:
        with self.db.connect() as conn:
            return self._read_conn(conn)

    def _write_conn(self, conn, data: Dict[str, Any]) -> None:
        existing_pairs = {
            (str(row["profile_name"]), str(row["server_key"]))
            for row in conn.execute("SELECT profile_name, server_key FROM awg_server_configs").fetchall()
        }
        target_pairs: set[tuple[str, str]] = set()
        for profile_name in sorted(data.keys()):
            profile = data.get(profile_name)
            if not isinstance(profile, dict):
                continue
            servers = profile.get("servers")
            if not isinstance(servers, dict):
                server_key = profile.get("server_key") or profile.get("region")
                if isinstance(server_key, str) and server_key:
                    servers = {server_key: profile}
                else:
                    servers = {}
            for server_key, server_entry in servers.items():
                if not isinstance(server_entry, dict):
                    continue
                target_pairs.add((str(profile_name), str(server_key)))
                conn.execute(
                    """
                    INSERT INTO awg_server_configs(profile_name, server_key, config_text, wg_conf, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(profile_name, server_key) DO UPDATE SET
                        config_text=excluded.config_text,
                        wg_conf=excluded.wg_conf,
                        created_at=excluded.created_at
                    """,
                    (
                        profile_name,
                        str(server_key),
                        str(server_entry.get("config") or ""),
                        server_entry.get("wg_conf"),
                        server_entry.get("created_at"),
                    ),
                )
        stale_pairs = sorted(existing_pairs - target_pairs)
        for profile_name, server_key in stale_pairs:
            conn.execute(
                "DELETE FROM awg_server_configs WHERE profile_name = ? AND server_key = ?",
                (profile_name, server_key),
            )

    def write(self, data: Dict[str, Any]) -> None:
        with self.db.transaction() as conn:
            self._write_conn(conn, data)

    def update(self, mutator: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
        with self.db.transaction() as conn:
            data = self._read_conn(conn)
            new_data = mutator(data)
            if not isinstance(new_data, dict):
                raise ValueError("SQLiteAWGStore.update mutator must return dict")
            self._write_conn(conn, new_data)
            return new_data
