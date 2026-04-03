from __future__ import annotations

from config import MENU_TITLE, SQLITE_DB_PATH, UPDATE_BRANCH
from db.schema import ensure_schema
from db.sqlite_db import SQLiteDB
from utils.security import escape_markdown


_db = SQLiteDB(SQLITE_DB_PATH)
_GLOBAL_TELEMETRY_KEY = "telemetry_enabled_global"
_BOT_MENU_TITLE_KEY = "bot_menu_title"
_ACCESS_REQUESTS_ENABLED_KEY = "access_requests_enabled"
_ACCESS_GATE_MESSAGE_KEY = "access_gate_message"
_INITIAL_SETUP_STATE_KEY = "initial_setup_state"
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
_UPDATES_LOCAL_VERSION_KEY = "updates_local_version"
_UPDATES_REMOTE_VERSION_KEY = "updates_remote_version"
_BACKUPS_ENABLED_KEY = "backups_enabled"
_BACKUPS_INTERVAL_HOURS_KEY = "backups_interval_hours"
_BACKUPS_KEEP_COUNT_KEY = "backups_keep_count"
_BACKUPS_LAST_RUN_AT_KEY = "backups_last_run_at"
_BACKUPS_LAST_STATUS_KEY = "backups_last_status"
_BACKUPS_LAST_ERROR_KEY = "backups_last_error"
_BACKUPS_LAST_SNAPSHOT_PATH_KEY = "backups_last_snapshot_path"
_BACKUPS_LAST_SNAPSHOT_SHA256_KEY = "backups_last_snapshot_sha256"
_BACKUPS_LAST_RESTORE_AT_KEY = "backups_last_restore_at"
_BACKUPS_LAST_RESTORE_STATUS_KEY = "backups_last_restore_status"
_schema_ready = False


def _ensure_runtime_schema() -> None:
    global _schema_ready
    if _schema_ready:
        with _db.connect() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'"
            ).fetchone()
        if row:
            return
        _schema_ready = False
    with _db.transaction() as conn:
        ensure_schema(conn)
    _schema_ready = True


def is_global_telemetry_enabled() -> bool:
    _ensure_runtime_schema()
    with _db.connect() as conn:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?",
            (_GLOBAL_TELEMETRY_KEY,),
        ).fetchone()
    return bool(row) and str(row["value"]).strip() == "1"


def _meta_get(key: str, default: str = "") -> str:
    _ensure_runtime_schema()
    with _db.connect() as conn:
        row = conn.execute("SELECT value FROM schema_meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]).strip() if row and row["value"] is not None else default


def _meta_set(key: str, value: str) -> str:
    _ensure_runtime_schema()
    normalized = str(value or "")
    with _db.transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)", (key, normalized))
    return normalized


def set_global_telemetry_enabled(enabled: bool) -> bool:
    _ensure_runtime_schema()
    value = "1" if enabled else "0"
    with _db.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            (_GLOBAL_TELEMETRY_KEY, value),
        )
    return enabled


def get_menu_title() -> str:
    _ensure_runtime_schema()
    with _db.connect() as conn:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?",
            (_BOT_MENU_TITLE_KEY,),
        ).fetchone()
    value = str(row["value"]).strip() if row and row["value"] is not None else ""
    return value or MENU_TITLE


def get_menu_title_markdown() -> str:
    return escape_markdown(get_menu_title())


def set_menu_title(value: str) -> str:
    _ensure_runtime_schema()
    normalized = str(value or "").replace("\r", " ").replace("\n", " ").strip() or MENU_TITLE
    with _db.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            (_BOT_MENU_TITLE_KEY, normalized),
        )
    return normalized


def are_access_requests_enabled() -> bool:
    return _meta_get(_ACCESS_REQUESTS_ENABLED_KEY, "1") == "1"


def set_access_requests_enabled(enabled: bool) -> bool:
    _meta_set(_ACCESS_REQUESTS_ENABLED_KEY, "1" if enabled else "0")
    return enabled


def get_access_gate_message() -> str:
    value = _meta_get(_ACCESS_GATE_MESSAGE_KEY, "").strip()
    return value or "Authorization required."


def set_access_gate_message(value: str) -> str:
    normalized = str(value or "").replace("\r", " ").replace("\n", " ").strip() or "Authorization required."
    _meta_set(_ACCESS_GATE_MESSAGE_KEY, normalized)
    return normalized


def _count_rows(table: str) -> int:
    _ensure_runtime_schema()
    with _db.connect() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
    return int(row["c"]) if row and row["c"] is not None else 0


def has_any_servers() -> bool:
    return _count_rows("servers") > 0


def get_initial_setup_state() -> str:
    _ensure_runtime_schema()
    with _db.connect() as conn:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?",
            (_INITIAL_SETUP_STATE_KEY,),
        ).fetchone()
    value = str(row["value"]).strip().lower() if row and row["value"] is not None else ""
    if value in {"dismissed", "completed"}:
        return value
    return "pending"


def set_initial_setup_state(state: str) -> str:
    normalized = str(state or "").strip().lower()
    if normalized not in {"pending", "dismissed", "completed"}:
        raise ValueError("Unsupported initial setup state")
    _ensure_runtime_schema()
    with _db.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            (_INITIAL_SETUP_STATE_KEY, normalized),
        )
    return normalized


def should_show_initial_admin_setup() -> bool:
    return get_initial_setup_state() == "pending" and not has_any_servers()


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
    _meta_set(_UPDATES_BRANCH_KEY, normalized)
    return normalized


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


def is_backups_enabled() -> bool:
    return _meta_get(_BACKUPS_ENABLED_KEY, "0") == "1"


def set_backups_enabled(enabled: bool) -> bool:
    _meta_set(_BACKUPS_ENABLED_KEY, "1" if enabled else "0")
    return enabled


def get_backups_interval_hours() -> int:
    raw = _meta_get(_BACKUPS_INTERVAL_HOURS_KEY, "24").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 24
    return value if value in {6, 12, 24} else 24


def set_backups_interval_hours(hours: int) -> int:
    value = int(hours)
    if value not in {6, 12, 24}:
        raise ValueError("Unsupported backups interval")
    _meta_set(_BACKUPS_INTERVAL_HOURS_KEY, str(value))
    return value


def get_backups_keep_count() -> int:
    raw = _meta_get(_BACKUPS_KEEP_COUNT_KEY, "10").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 10
    return value if value in {5, 10, 20} else 10


def set_backups_keep_count(count: int) -> int:
    value = int(count)
    if value not in {5, 10, 20}:
        raise ValueError("Unsupported backups keep count")
    _meta_set(_BACKUPS_KEEP_COUNT_KEY, str(value))
    return value


def record_backup_run(status: str, run_at: str, *, error: str = "", snapshot_path: str = "", snapshot_sha256: str = "") -> None:
    _meta_set(_BACKUPS_LAST_RUN_AT_KEY, run_at)
    _meta_set(_BACKUPS_LAST_STATUS_KEY, status)
    _meta_set(_BACKUPS_LAST_ERROR_KEY, error)
    if snapshot_path:
        _meta_set(_BACKUPS_LAST_SNAPSHOT_PATH_KEY, snapshot_path)
    if snapshot_sha256:
        _meta_set(_BACKUPS_LAST_SNAPSHOT_SHA256_KEY, snapshot_sha256)


def record_backup_restore(status: str, restored_at: str, *, error: str = "") -> None:
    _meta_set(_BACKUPS_LAST_RESTORE_AT_KEY, restored_at)
    _meta_set(_BACKUPS_LAST_RESTORE_STATUS_KEY, status)
    if error:
        _meta_set(_BACKUPS_LAST_ERROR_KEY, error)


def get_backups_state() -> dict[str, str | int | bool]:
    return {
        "enabled": is_backups_enabled(),
        "interval_hours": get_backups_interval_hours(),
        "keep_count": get_backups_keep_count(),
        "last_run_at": _meta_get(_BACKUPS_LAST_RUN_AT_KEY, ""),
        "last_status": _meta_get(_BACKUPS_LAST_STATUS_KEY, "never"),
        "last_error": _meta_get(_BACKUPS_LAST_ERROR_KEY, ""),
        "last_snapshot_path": _meta_get(_BACKUPS_LAST_SNAPSHOT_PATH_KEY, ""),
        "last_snapshot_sha256": _meta_get(_BACKUPS_LAST_SNAPSHOT_SHA256_KEY, ""),
        "last_restore_at": _meta_get(_BACKUPS_LAST_RESTORE_AT_KEY, ""),
        "last_restore_status": _meta_get(_BACKUPS_LAST_RESTORE_STATUS_KEY, "never"),
    }
