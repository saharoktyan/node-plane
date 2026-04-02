from __future__ import annotations

import os
import subprocess
import logging
from datetime import datetime, timezone
from typing import Dict

from config import APP_ROOT, APP_VERSION, BASE_DIR, INSTALL_MODE, SOURCE_ROOT
from services import app_settings

UPDATE_UNIT_PREFIX = "node-plane-update"
_log = logging.getLogger("updates")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def detect_install_mode() -> str:
    mode = INSTALL_MODE.strip().lower()
    if mode in {"simple", "portable"}:
        return mode
    if "/current" in APP_ROOT:
        return "simple"
    return "portable"


def _effective_source_root() -> str:
    source = SOURCE_ROOT
    if detect_install_mode() == "simple" and source == APP_ROOT:
        sibling = f"{BASE_DIR}-src"
        if os.path.isdir(sibling):
            return sibling
    return source


def _script_path(name: str) -> str:
    return f"{APP_ROOT}/scripts/{name}"


def _run_cmd(args: list[str], *, cwd: str | None = None, timeout: int = 60, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd or APP_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _system_cmd(*args: str) -> list[str]:
    if os.geteuid() == 0:
        return list(args)
    return ["sudo", "-n", *args]


def _parse_show_output(output: str) -> Dict[str, str]:
    payload: Dict[str, str] = {}
    for line in (output or "").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        payload[key.strip()] = value.strip()
    return payload


def _trim_log_tail(text: str, limit: int = 1200) -> str:
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[-limit:]


def _last_run_status_from_show(payload: Dict[str, str], fallback: str) -> str:
    active = payload.get("ActiveState", "").strip().lower()
    sub = payload.get("SubState", "").strip().lower()
    result = payload.get("Result", "").strip().lower()
    exec_status = payload.get("ExecMainStatus", "").strip()
    if active in {"activating", "active", "reloading", "deactivating"} or sub in {"start", "start-post", "running"}:
        return "running"
    if result == "success" or (active == "inactive" and exec_status in {"", "0"}):
        return "success"
    if result and result not in {"success", "unset"}:
        return "failed"
    if exec_status not in {"", "0"}:
        return "failed"
    return fallback


def _parse_check_output(output: str, returncode: int) -> Dict[str, str]:
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    status = "error"
    if lines and lines[0].startswith("CHECK_UPDATES|"):
        status = lines[0].split("|", 1)[1].strip() or "error"
    payload: Dict[str, str] = {"status": status}
    for line in lines[1:]:
        if ": " not in line:
            continue
        key, value = line.split(": ", 1)
        payload[key.strip()] = value.strip()
    if returncode != 0 and status != "error":
        payload["status"] = "error"
    return payload


def check_for_updates(timeout: int = 60) -> Dict[str, str]:
    try:
        env = os.environ.copy()
        env["NODE_PLANE_SOURCE_DIR"] = _effective_source_root()
        proc = _run_cmd([_script_path("check_updates.sh")], timeout=timeout, env=env)
        output = (proc.stdout or "").strip()
        if proc.stderr:
            output = f"{output}\n{proc.stderr.strip()}".strip()
        result = _parse_check_output(output, proc.returncode)
    except Exception as exc:
        result = {"status": "error", "message": str(exc)}
    result.setdefault("local_label", APP_VERSION)
    result.setdefault("remote_label", result.get("local_label", APP_VERSION))
    result.setdefault("source_dir", _effective_source_root())
    result["checked_at"] = _utcnow_iso()
    app_settings.record_update_check(result)
    return result


def is_manual_update_supported() -> bool:
    return detect_install_mode() == "simple" and os.path.isfile(_script_path("update.sh"))


def refresh_update_run_state(timeout: int = 20) -> Dict[str, str]:
    state = app_settings.get_update_state()
    unit_name = str(state.get("last_run_unit") or "").strip()
    if not unit_name:
        return state
    try:
        show_proc = _run_cmd(
            _system_cmd(
                "systemctl",
                "show",
                f"{unit_name}.service",
                "--property=LoadState,ActiveState,SubState,Result,ExecMainStatus",
                "--no-pager",
            ),
            timeout=timeout,
        )
        show_payload = _parse_show_output((show_proc.stdout or "").strip())
        if show_proc.returncode == 0:
            status = _last_run_status_from_show(show_payload, str(state.get("last_run_status") or "never"))
        else:
            status = str(state.get("last_run_status") or "never")
        journal_proc = _run_cmd(
            _system_cmd("journalctl", "-u", f"{unit_name}.service", "-n", "40", "--no-pager"),
            timeout=timeout,
        )
        log_tail = _trim_log_tail((journal_proc.stdout or "").strip())
        if status == "running":
            if log_tail:
                app_settings.set_update_run_log_tail(log_tail)
            state = app_settings.get_update_state()
            state["last_run_status"] = status
            state["last_run_log_tail"] = log_tail or str(state.get("last_run_log_tail") or "")
            return state
        if status in {"success", "failed"} and state.get("last_run_status") != status:
            app_settings.record_update_run_finished(status, _utcnow_iso(), log_tail if status == "failed" else "")
            state = app_settings.get_update_state()
            state["last_run_unit"] = unit_name
            return state
        if status == "failed" and log_tail and state.get("last_run_log_tail") != log_tail:
            app_settings.set_update_run_log_tail(log_tail)
            state = app_settings.get_update_state()
            state["last_run_unit"] = unit_name
        return state
    except Exception as exc:
        if str(state.get("last_run_status") or "") == "running":
            app_settings.record_update_run_finished("failed", _utcnow_iso(), str(exc))
            state = app_settings.get_update_state()
        return state


def schedule_update(timeout: int = 30) -> Dict[str, str]:
    state = refresh_update_run_state()
    if str(state.get("last_run_status") or "") == "running":
        return {"status": "running", "unit_name": str(state.get("last_run_unit") or "")}
    if not is_manual_update_supported():
        app_settings.record_update_run_finished("failed", _utcnow_iso(), "manual updates are only available in simple mode")
        state = app_settings.get_update_state()
        return {"status": "failed", "message": str(state.get("last_run_log_tail") or "")}
    source_root = _effective_source_root()
    started_at = _utcnow_iso()
    unit_name = f"{UPDATE_UNIT_PREFIX}-{started_at.replace(':', '').replace('-', '').replace('T', '-').replace('Z', '').lower()}"
    try:
        cmd = _system_cmd(
            "systemd-run",
            "--unit",
            unit_name,
            "--collect",
            "--working-directory",
            source_root,
            "--setenv",
            f"NODE_PLANE_SOURCE_DIR={source_root}",
            "--setenv",
            "NODE_PLANE_INSTALL_MODE=simple",
            f"{source_root}/scripts/update.sh",
            "--mode",
            "simple",
        )
        proc = _run_cmd(cmd, cwd=source_root, timeout=timeout)
        output = ((proc.stdout or "").strip() + "\n" + (proc.stderr or "").strip()).strip()
        if proc.returncode != 0:
            message = output or f"failed to start update job (exit {proc.returncode})"
            app_settings.record_update_run_finished("failed", _utcnow_iso(), message)
            return {"status": "failed", "message": message}
        app_settings.record_update_run_started(started_at, unit_name)
        return {"status": "running", "unit_name": unit_name}
    except Exception as exc:
        app_settings.record_update_run_finished("failed", _utcnow_iso(), str(exc))
        return {"status": "failed", "message": str(exc)}


def auto_check_job(context: object | None = None) -> None:
    if not app_settings.is_updates_auto_check_enabled():
        return
    try:
        result = check_for_updates()
        status = str(result.get("status") or "error")
        if status == "available":
            _log.info("Auto-check found an available update: %s", result.get("remote_label") or result.get("remote_version") or "unknown")
        elif status == "error":
            _log.warning("Auto-check failed: %s", result.get("message") or "unknown error")
        else:
            _log.info("Auto-check completed: %s", status)
    except Exception:
        _log.exception("Auto-check job failed")


def get_updates_overview() -> Dict[str, str | bool]:
    state = refresh_update_run_state()
    current_version = APP_VERSION
    remote_label = state.get("remote_label", APP_VERSION)
    update_available = state.get("update_available", "0") == "1"
    last_status = state.get("last_status", "never")
    if remote_label and remote_label == current_version:
        update_available = False
        if last_status == "available":
            last_status = "up_to_date"
    return {
        "install_mode": detect_install_mode(),
        "current_version": current_version,
        "source_dir": _effective_source_root(),
        "update_supported": is_manual_update_supported(),
        "auto_check_enabled": app_settings.is_updates_auto_check_enabled(),
        "last_checked_at": state.get("last_checked_at", ""),
        "last_status": last_status,
        "update_available": update_available,
        "local_label": state.get("local_label", APP_VERSION),
        "remote_label": remote_label,
        "upstream_ref": state.get("upstream_ref", ""),
        "last_error": state.get("last_error", ""),
        "last_run_started_at": state.get("last_run_started_at", ""),
        "last_run_finished_at": state.get("last_run_finished_at", ""),
        "last_run_status": state.get("last_run_status", "never"),
        "last_run_log_tail": state.get("last_run_log_tail", ""),
        "last_run_unit": state.get("last_run_unit", ""),
    }


def get_updates_menu_emoji(overview: Dict[str, str | bool] | None = None) -> str:
    overview = overview or get_updates_overview()
    last_run_status = str(overview.get("last_run_status") or "")
    if last_run_status == "running":
        return "⏳"
    if last_run_status == "failed":
        return "⚠️"
    if bool(overview.get("update_available")):
        return "🆕"
    if str(overview.get("last_status") or "") == "up_to_date":
        return "🟢"
    return "📦"
