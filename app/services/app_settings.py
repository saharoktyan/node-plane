"""Persistent update metadata shared by backend maintenance services."""
from __future__ import annotations

from config import UPDATE_BRANCH

from db import ensure_schema, get_db

_db = get_db()

_UPDATES_AUTO_CHECK_KEY = "updates_auto_check_enabled"

_UPDATES_LAST_CHECKED_AT_KEY = "updates_last_checked_at"

_UPDATES_LAST_STATUS_KEY = "updates_last_status"

_UPDATES_UPDATE_AVAILABLE_KEY = "updates_update_available"

_UPDATES_LOCAL_LABEL_KEY = "updates_local_label"

_UPDATES_REMOTE_LABEL_KEY = "updates_remote_label"

_UPDATES_UPSTREAM_REF_KEY = "updates_upstream_ref"

_UPDATES_LAST_ERROR_KEY = "updates_last_error"

_UPDATES_LAST_RUN_STARTED_AT_KEY = "updates_last_run_started_at"

_UPDATES_LAST_RUN_FINISHED_AT_KEY = "updates_last_run_finished_at"

_UPDATES_LAST_RUN_STATUS_KEY = "updates_last_run_status"

_UPDATES_LAST_RUN_LOG_TAIL_KEY = "updates_last_run_log_tail"

_UPDATES_LAST_RUN_UNIT_KEY = "updates_last_run_unit"

_UPDATES_BRANCH_KEY = "updates_branch"

_UPDATES_DEV_TRACK_KEY = "updates_dev_track"

_UPDATES_LOCAL_VERSION_KEY = "updates_local_version"

_UPDATES_REMOTE_VERSION_KEY = "updates_remote_version"

_schema_ready = False

_META_UPSERT_SQL = """
INSERT INTO schema_meta(key, value)
VALUES (?, ?)
ON CONFLICT(key) DO UPDATE SET value = excluded.value
"""

def _ensure_runtime_schema() -> None:
    global _schema_ready
    with _db.transaction() as conn:
        ensure_schema(conn)
    _schema_ready = True

def _meta_get(key: str, default: str = "") -> str:
    _ensure_runtime_schema()
    with _db.connect() as conn:
        row = conn.execute("SELECT value FROM schema_meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]).strip() if row and row["value"] is not None else default

def _meta_set(key: str, value: str) -> str:
    _ensure_runtime_schema()
    normalized = str(value or "")
    with _db.transaction() as conn:
        conn.execute(_META_UPSERT_SQL, (key, normalized))
    return normalized

def is_updates_auto_check_enabled() -> bool:
    return _meta_get(_UPDATES_AUTO_CHECK_KEY, "0") == "1"

def set_updates_auto_check_enabled(enabled: bool) -> bool:
    _meta_set(_UPDATES_AUTO_CHECK_KEY, "1" if enabled else "0")
    return enabled

def get_updates_branch() -> str:
    value = _meta_get(_UPDATES_BRANCH_KEY, UPDATE_BRANCH or "main").strip().lower()
    return value if value in {"main", "dev"} else "main"

def set_updates_branch(branch: str) -> str:
    normalized = str(branch or "").strip().lower()
    if normalized not in {"main", "dev"}:
        raise ValueError("Unsupported updates branch")
    if normalized != get_updates_branch():
        _clear_update_check()
    _meta_set(_UPDATES_BRANCH_KEY, normalized)
    return normalized

def get_updates_dev_track() -> str:
    value = _meta_get(_UPDATES_DEV_TRACK_KEY, "tag").strip().lower()
    return value if value in {"tag", "head"} else "tag"

def set_updates_dev_track(track: str) -> str:
    normalized = str(track or "").strip().lower()
    if normalized not in {"tag", "head"}:
        raise ValueError("Unsupported updates dev track")
    if normalized != get_updates_dev_track():
        _clear_update_check()
    _meta_set(_UPDATES_DEV_TRACK_KEY, normalized)
    return normalized

def _clear_update_check() -> None:
    for key, value in (
        (_UPDATES_LAST_CHECKED_AT_KEY, ""),
        (_UPDATES_LAST_STATUS_KEY, "never"),
        (_UPDATES_UPDATE_AVAILABLE_KEY, "0"),
        (_UPDATES_REMOTE_VERSION_KEY, ""),
        (_UPDATES_REMOTE_LABEL_KEY, ""),
        (_UPDATES_UPSTREAM_REF_KEY, ""),
    ):
        _meta_set(key, value)

def record_update_check(result: dict[str, str]) -> None:
    _meta_set(_UPDATES_LAST_CHECKED_AT_KEY, result.get("checked_at", ""))
    _meta_set(_UPDATES_LAST_STATUS_KEY, result.get("status", "error"))
    _meta_set(_UPDATES_UPDATE_AVAILABLE_KEY, "1" if result.get("status") == "available" else "0")
    _meta_set(_UPDATES_BRANCH_KEY, result.get("branch", get_updates_branch()))
    _meta_set(_UPDATES_LOCAL_VERSION_KEY, result.get("local_version", ""))
    _meta_set(_UPDATES_REMOTE_VERSION_KEY, result.get("remote_version", ""))
    _meta_set(_UPDATES_LOCAL_LABEL_KEY, result.get("local_label", ""))
    _meta_set(_UPDATES_REMOTE_LABEL_KEY, result.get("remote_label", ""))
    _meta_set(_UPDATES_UPSTREAM_REF_KEY, result.get("upstream_ref", ""))
    _meta_set(_UPDATES_LAST_ERROR_KEY, result.get("message", ""))

def get_update_state() -> dict[str, str]:
    return {
        "branch": get_updates_branch(),
        "dev_track": get_updates_dev_track(),
        "last_checked_at": _meta_get(_UPDATES_LAST_CHECKED_AT_KEY, ""),
        "last_status": _meta_get(_UPDATES_LAST_STATUS_KEY, "never"),
        "update_available": _meta_get(_UPDATES_UPDATE_AVAILABLE_KEY, "0"),
        "local_version": _meta_get(_UPDATES_LOCAL_VERSION_KEY, ""),
        "remote_version": _meta_get(_UPDATES_REMOTE_VERSION_KEY, ""),
        "local_label": _meta_get(_UPDATES_LOCAL_LABEL_KEY, ""),
        "remote_label": _meta_get(_UPDATES_REMOTE_LABEL_KEY, ""),
        "upstream_ref": _meta_get(_UPDATES_UPSTREAM_REF_KEY, ""),
        "last_error": _meta_get(_UPDATES_LAST_ERROR_KEY, ""),
        "last_run_started_at": _meta_get(_UPDATES_LAST_RUN_STARTED_AT_KEY, ""),
        "last_run_finished_at": _meta_get(_UPDATES_LAST_RUN_FINISHED_AT_KEY, ""),
        "last_run_status": _meta_get(_UPDATES_LAST_RUN_STATUS_KEY, "never"),
        "last_run_log_tail": _meta_get(_UPDATES_LAST_RUN_LOG_TAIL_KEY, ""),
        "last_run_unit": _meta_get(_UPDATES_LAST_RUN_UNIT_KEY, ""),
    }

def record_update_run_started(started_at: str, unit_name: str) -> None:
    _meta_set(_UPDATES_LAST_RUN_STARTED_AT_KEY, started_at)
    _meta_set(_UPDATES_LAST_RUN_FINISHED_AT_KEY, "")
    _meta_set(_UPDATES_LAST_RUN_STATUS_KEY, "running")
    _meta_set(_UPDATES_LAST_RUN_LOG_TAIL_KEY, "")
    _meta_set(_UPDATES_LAST_RUN_UNIT_KEY, unit_name)

def record_update_run_finished(status: str, finished_at: str, log_tail: str = "") -> None:
    _meta_set(_UPDATES_LAST_RUN_FINISHED_AT_KEY, finished_at)
    _meta_set(_UPDATES_LAST_RUN_STATUS_KEY, status)
    _meta_set(_UPDATES_LAST_RUN_LOG_TAIL_KEY, log_tail)

def set_update_run_log_tail(log_tail: str) -> None:
    _meta_set(_UPDATES_LAST_RUN_LOG_TAIL_KEY, log_tail)
