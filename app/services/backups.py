from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

from config import APP_VERSION, SHARED_ROOT, SQLITE_DB_PATH
from services import app_settings

_log = logging.getLogger("backups")
_BACKUP_DIR = Path(SHARED_ROOT) / "backups" / "db"
_BACKUP_EXT = ".sqlite3"
_META_EXT = ".meta.json"


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


def _source_signature() -> tuple[str, int]:
    import sqlite3

    h = hashlib.sha256()
    total_size = 0
    if os.path.exists(SQLITE_DB_PATH):
        total_size += os.path.getsize(SQLITE_DB_PATH)
    if os.path.exists(f"{SQLITE_DB_PATH}-wal"):
        total_size += os.path.getsize(f"{SQLITE_DB_PATH}-wal")
    conn = sqlite3.connect(SQLITE_DB_PATH, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        table_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        for table_row in table_rows:
            table = str(table_row["name"])
            h.update(f"table:{table}\n".encode("utf-8"))
            col_rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
            columns = [str(item["name"]) for item in col_rows]
            if not columns:
                continue
            order_clause = ", ".join(columns)
            if table == "schema_meta":
                sql = f"SELECT * FROM {table} WHERE key NOT LIKE 'backups_%' ORDER BY {order_clause}"
            else:
                sql = f"SELECT * FROM {table} ORDER BY {order_clause}"
            for row in conn.execute(sql).fetchall():
                payload = {col: row[col] for col in columns}
                h.update(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))
                h.update(b"\n")
    finally:
        conn.close()
    return h.hexdigest(), total_size


def _copy_sqlite_snapshot(target: Path) -> None:
    import sqlite3

    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    source = sqlite3.connect(SQLITE_DB_PATH, timeout=5.0)
    try:
        source.execute("PRAGMA busy_timeout = 5000")
        source.execute("PRAGMA wal_checkpoint(PASSIVE)")
        dest = sqlite3.connect(str(target))
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass


def _load_meta(meta_path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_meta(meta_path: Path, payload: Dict[str, Any]) -> None:
    meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        os.chmod(meta_path, 0o600)
    except OSError:
        pass


def list_backups() -> List[Dict[str, Any]]:
    get_backup_dir()
    items: List[Dict[str, Any]] = []
    for path in sorted(_BACKUP_DIR.glob(f"*{_BACKUP_EXT}"), reverse=True):
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
    get_backup_dir()
    created_at = _utcnow_iso()
    backup_path = _backup_path(_snapshot_name())
    try:
        source_fingerprint, source_size = _source_signature()
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
        _copy_sqlite_snapshot(backup_path)
        sha256 = _sha256_file(backup_path)
        payload = {
            "created_at": created_at,
            "trigger": str(trigger or "manual"),
            "note": str(note or ""),
            "sha256": sha256,
            "app_version": APP_VERSION,
            "db_path": SQLITE_DB_PATH,
            "source_fingerprint": source_fingerprint,
            "source_size": source_size,
        }
        _write_meta(_meta_path_for_backup(backup_path), payload)
        prune_backups()
        app_settings.record_backup_run(
            "success",
            created_at,
            snapshot_path=str(backup_path),
            snapshot_sha256=sha256,
        )
        return {
            "status": "success",
            "created_at": created_at,
            "path": str(backup_path),
            "name": backup_path.name,
            "sha256": sha256,
            "trigger": payload["trigger"],
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
    info = get_backup_info(name)
    restored_at = _utcnow_iso()
    if not info:
        app_settings.record_backup_restore("failed", restored_at, error="backup not found")
        return {"status": "failed", "message": "backup not found"}
    try:
        import sqlite3

        pre_restore = create_backup("pre_restore")
        source = sqlite3.connect(str(info["path"]), timeout=5.0)
        try:
            dest = sqlite3.connect(SQLITE_DB_PATH, timeout=5.0)
            try:
                source.backup(dest)
                dest.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                dest.close()
        finally:
            source.close()
        try:
            os.chmod(SQLITE_DB_PATH, 0o600)
        except OSError:
            pass
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
