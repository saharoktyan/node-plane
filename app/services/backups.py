from __future__ import annotations

import hashlib
import json
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

from config import APP_VERSION, DB_BACKEND, SHARED_ROOT
from db import ensure_schema, get_db
from db.migrate_sqlite_to_postgres import ALERT_STATE_COLUMNS, ALERT_STATE_DDL, TABLE_COLUMNS
from services import app_settings

_log = logging.getLogger("backups")
_BACKUP_DIR = Path(SHARED_ROOT) / "backups" / "db"
_BACKUP_EXT = ".json"
_META_EXT = ".meta.json"


def _postgres_backups_supported() -> bool:
    return DB_BACKEND == "postgres"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def get_backup_dir() -> str:
    _BACKUP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    return str(_BACKUP_DIR)


def clear_backup_storage() -> Dict[str, Any]:
    get_backup_dir()
    removed = 0
    for path in list(_BACKUP_DIR.iterdir()):
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=False)
            else:
                path.unlink(missing_ok=True)
            removed += 1
        except OSError:
            _log.warning("Failed to remove backup storage entry %s", path, exc_info=True)
    return {"removed": removed, "path": str(_BACKUP_DIR)}


def _snapshot_name(ts: datetime | None = None) -> str:
    stamp = (ts or _utcnow()).strftime("%Y-%m-%dT%H-%M-%S-%fZ")
    return f"bot-{stamp}{_BACKUP_EXT}"


def _backup_path(name: str) -> Path:
    return _BACKUP_DIR / name


def _meta_path_for_backup(path: Path) -> Path:
    return path.with_suffix(path.suffix + _META_EXT)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def backup_token(name: str) -> str:
    raw = Path(str(name or "")).name
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _load_meta(meta_path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_meta(meta_path: Path, payload: Dict[str, Any]) -> None:
    meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        meta_path.chmod(0o600)
    except OSError:
        pass


def _table_exists(conn, name: str) -> bool:
    try:
        conn.execute(f"SELECT 1 FROM {name} WHERE 1 = 0").fetchall()
        return True
    except Exception:
        return False


def _backup_tables(conn) -> list[tuple[str, list[str]]]:
    tables = list(TABLE_COLUMNS)
    if _table_exists(conn, "alert_state"):
        tables.append(("alert_state", list(ALERT_STATE_COLUMNS)))
    return tables


def _ordered_select_sql(table: str, columns: list[str]) -> str:
    selected = ", ".join(columns)
    order_by = ", ".join(columns)
    return f"SELECT {selected} FROM {table} ORDER BY {order_by}"


def _rows_for_table(conn, table: str, columns: list[str]) -> list[dict[str, Any]]:
    if table == "schema_meta":
        rows = conn.execute(
            "SELECT key, value FROM schema_meta WHERE key NOT LIKE ? ORDER BY key",
            ("backups_%",),
        ).fetchall()
        return [{"key": row["key"], "value": row["value"]} for row in rows]
    rows = conn.execute(_ordered_select_sql(table, columns)).fetchall()
    return [{column: row[column] for column in columns} for row in rows]


def _build_backup_payload() -> tuple[dict[str, Any], str, int]:
    db = get_db()
    with db.connect() as conn:
        ensure_schema(conn)
        payload = {
            "format_version": 1,
            "backend": "postgres",
            "tables": {},
        }
        for table, columns in _backup_tables(conn):
            payload["tables"][table] = _rows_for_table(conn, table, columns)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return payload, hashlib.sha256(encoded).hexdigest(), len(encoded)


def _write_backup_payload(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _load_backup_payload(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("backup payload is invalid")
    if str(data.get("backend") or "") != "postgres":
        raise ValueError("backup payload backend is invalid")
    tables = data.get("tables")
    if not isinstance(tables, dict):
        raise ValueError("backup payload tables are invalid")
    return data


def _clear_target_tables(conn, backup_tables: list[str]) -> None:
    ordered = [table for table, _columns in TABLE_COLUMNS if table in backup_tables]
    if "alert_state" in backup_tables or _table_exists(conn, "alert_state"):
        ordered.append("alert_state")
    for table in reversed(ordered):
        if _table_exists(conn, table):
            conn.execute(f"DELETE FROM {table}")


def _insert_backup_rows(conn, table: str, columns: list[str], rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    placeholders = ", ".join("?" for _ in columns)
    selected = ", ".join(columns)
    sql = f"INSERT INTO {table}({selected}) VALUES ({placeholders})"
    for row in rows:
        conn.execute(sql, tuple(row.get(column) for column in columns))


def list_backups() -> List[Dict[str, Any]]:
    get_backup_dir()
    items: List[Dict[str, Any]] = []
    for path in sorted(_BACKUP_DIR.glob(f"*{_BACKUP_EXT}"), reverse=True):
        if path.name.endswith(_META_EXT):
            continue
        meta = _load_meta(_meta_path_for_backup(path))
        item = {
            "name": path.name,
            "path": str(path),
            "size_bytes": path.stat().st_size if path.exists() else 0,
            "created_at": meta.get("created_at") or "",
            "trigger": meta.get("trigger") or "unknown",
            "sha256": meta.get("sha256") or "",
            "app_version": meta.get("app_version") or "",
            "note": meta.get("note") or "",
            "backend": meta.get("backend") or "postgres",
        }
        items.append(item)
    items.sort(key=lambda item: str(item.get("created_at") or item.get("name") or ""), reverse=True)
    return items


def prune_backups(keep_count: int | None = None) -> Dict[str, Any]:
    keep = int(keep_count or app_settings.get_backups_keep_count())
    items = list_backups()
    removed: List[str] = []
    for item in items[keep:]:
        path = Path(str(item["path"]))
        meta_path = _meta_path_for_backup(path)
        try:
            if path.exists():
                path.unlink()
            if meta_path.exists():
                meta_path.unlink()
            removed.append(path.name)
        except OSError:
            _log.warning("Failed to prune backup %s", path.name, exc_info=True)
    return {"removed": removed, "kept": min(len(items), keep)}


def create_backup(trigger: str = "manual", note: str = "") -> Dict[str, Any]:
    created_at = _utcnow_iso()
    if not _postgres_backups_supported():
        return {
            "status": "unsupported",
            "created_at": created_at,
            "message": "Backups are supported only when DB_BACKEND=postgres",
        }
    get_backup_dir()
    backup_path = _backup_path(_snapshot_name())
    try:
        payload, source_fingerprint, source_size = _build_backup_payload()
        latest = list_backups()
        if latest:
            latest_meta = _load_meta(_meta_path_for_backup(Path(str(latest[0]["path"]))))
            if (
                str(latest_meta.get("source_fingerprint") or "") == source_fingerprint
                and int(latest_meta.get("source_size") or 0) == source_size
            ):
                app_settings.record_backup_run(
                    "skipped_duplicate",
                    created_at,
                    snapshot_path=str(latest[0].get("path") or ""),
                    snapshot_sha256=str(latest[0].get("sha256") or ""),
                )
                return {
                    "status": "skipped_duplicate",
                    "created_at": created_at,
                    "path": str(latest[0].get("path") or ""),
                    "sha256": str(latest[0].get("sha256") or ""),
                }
        _write_backup_payload(backup_path, payload)
        sha256 = _sha256_file(backup_path)
        meta = {
            "created_at": created_at,
            "trigger": str(trigger or "manual"),
            "note": str(note or ""),
            "sha256": sha256,
            "app_version": APP_VERSION,
            "backend": "postgres",
            "source_fingerprint": source_fingerprint,
            "source_size": source_size,
        }
        _write_meta(_meta_path_for_backup(backup_path), meta)
        prune_backups()
        app_settings.record_backup_run("success", created_at, snapshot_path=str(backup_path), snapshot_sha256=sha256)
        return {
            "status": "success",
            "created_at": created_at,
            "path": str(backup_path),
            "name": backup_path.name,
            "sha256": sha256,
            "trigger": meta["trigger"],
        }
    except Exception as exc:
        app_settings.record_backup_run("failed", created_at, error=str(exc))
        return {"status": "failed", "created_at": created_at, "message": str(exc)}


def get_backup_info(name: str) -> Dict[str, Any] | None:
    backup_path = _backup_path(Path(name).name)
    if not backup_path.is_file():
        return None
    meta = _load_meta(_meta_path_for_backup(backup_path))
    return {
        "name": backup_path.name,
        "path": str(backup_path),
        "size_bytes": backup_path.stat().st_size,
        "created_at": meta.get("created_at") or "",
        "trigger": meta.get("trigger") or "unknown",
        "sha256": meta.get("sha256") or "",
        "app_version": meta.get("app_version") or "",
        "note": meta.get("note") or "",
        "backend": meta.get("backend") or "postgres",
    }


def resolve_backup_token(token: str) -> Dict[str, Any] | None:
    short = str(token or "").strip().lower()
    if not short:
        return None
    for item in list_backups():
        if backup_token(str(item.get("name") or "")) == short:
            return item
    return None


def restore_backup(name: str) -> Dict[str, Any]:
    restored_at = _utcnow_iso()
    if not _postgres_backups_supported():
        return {"status": "unsupported", "message": "Restore is supported only when DB_BACKEND=postgres"}
    info = get_backup_info(name)
    if not info:
        app_settings.record_backup_restore("failed", restored_at, error="backup not found")
        return {"status": "failed", "message": "backup not found"}
    try:
        pre_restore = create_backup("pre_restore")
        payload = _load_backup_payload(Path(str(info["path"])))
        tables = payload.get("tables") or {}
        if not isinstance(tables, dict):
            raise ValueError("backup payload tables are invalid")
        db = get_db()
        with db.transaction() as conn:
            ensure_schema(conn)
            if "alert_state" in tables and not _table_exists(conn, "alert_state"):
                conn.execute(ALERT_STATE_DDL)
            _clear_target_tables(conn, list(tables.keys()))
            for table, columns in TABLE_COLUMNS:
                if table in tables:
                    rows = tables.get(table)
                    if not isinstance(rows, list):
                        raise ValueError(f"backup payload for {table} is invalid")
                    _insert_backup_rows(conn, table, columns, rows)
            if "alert_state" in tables:
                rows = tables.get("alert_state")
                if not isinstance(rows, list):
                    raise ValueError("backup payload for alert_state is invalid")
                _insert_backup_rows(conn, "alert_state", list(ALERT_STATE_COLUMNS), rows)
        app_settings.record_backup_restore("success", restored_at)
        return {
            "status": "success",
            "restored_at": restored_at,
            "backup": info,
            "pre_restore_status": pre_restore.get("status", ""),
        }
    except Exception as exc:
        app_settings.record_backup_restore("failed", restored_at, error=str(exc))
        return {"status": "failed", "message": str(exc)}


def run_scheduled_backup_if_due() -> Dict[str, Any]:
    state = app_settings.get_backups_state()
    if not bool(state.get("enabled")):
        return {"status": "disabled"}
    last_run_at = _parse_iso(str(state.get("last_run_at") or ""))
    interval_hours = int(state.get("interval_hours") or 24)
    if last_run_at and _utcnow() < last_run_at + timedelta(hours=interval_hours):
        return {"status": "not_due"}
    return create_backup("scheduled")


def maybe_create_pre_action_backup(trigger: str) -> Dict[str, Any]:
    return create_backup(trigger)


def get_backups_overview() -> Dict[str, Any]:
    state = app_settings.get_backups_state()
    items = list_backups()
    total_size = sum(int(item.get("size_bytes") or 0) for item in items)
    return {
        **state,
        "backend": DB_BACKEND,
        "total_backups": len(items),
        "total_size_bytes": total_size,
        "latest_backup": items[0] if items else None,
    }


def auto_backup_job(context: object | None = None) -> None:
    try:
        result = run_scheduled_backup_if_due()
        status = str(result.get("status") or "")
        if status == "success":
            _log.info("Scheduled backup created: %s", result.get("name") or result.get("path") or "unknown")
        elif status == "failed":
            _log.warning("Scheduled backup failed: %s", result.get("message") or "unknown error")
    except Exception:
        _log.exception("Auto backup job failed")
