from __future__ import annotations

import os
import subprocess
import logging
import re
import threading
from datetime import datetime, timezone
from typing import Dict, List

from config import APP_ROOT, APP_SEMVER, APP_VERSION, BASE_DIR, INSTALL_MODE, SOURCE_ROOT
from services import app_settings
from services.backups import maybe_create_pre_action_backup

UPDATE_UNIT_PREFIX = "node-plane-update"
_log = logging.getLogger("updates")
_update_run_lock = threading.Lock()


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


def _parse_versions_output(output: str, returncode: int) -> Dict[str, object]:
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    status = "error"
    if lines and lines[0].startswith("LIST_VERSIONS|"):
        status = lines[0].split("|", 1)[1].strip() or "error"
    payload: Dict[str, object] = {"status": status, "versions": []}
    items: List[Dict[str, str]] = []
    for line in lines[1:]:
        if ": " not in line:
            continue
        key, value = line.split(": ", 1)
        key = key.strip()
        value = value.strip()
        if key == "version_item":
            version, ref, kind = (value.split("|", 2) + ["", ""])[:3]
            items.append({"version": version, "ref": ref, "kind": kind or "tag"})
            continue
        payload[key] = value
    payload["versions"] = items
    if returncode != 0 and status != "error":
        payload["status"] = "error"
    return payload


def _version_from_label(label: str) -> str:
    raw = str(label or "").strip()
    if not raw:
        return ""
    return raw.split(" · ", 1)[0].strip()


_SEMVER_RE = re.compile(r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)(?:-(?P<pre>[0-9A-Za-z.-]+))?$")


def _parse_semver(value: str) -> tuple[int, int, int, tuple[int, ...], str] | None:
    raw = str(value or "").strip()
    match = _SEMVER_RE.fullmatch(raw)
    if not match:
        return None
    pre = str(match.group("pre") or "")
    pre_parts: tuple[int, ...] = ()
    if pre.startswith("alpha."):
        suffix = pre.split(".", 1)[1]
        if suffix.isdigit():
            pre_parts = (0, int(suffix))
        else:
            pre_parts = (0, 0)
    elif pre:
        pre_parts = (1, 0)
    else:
        pre_parts = (2, 0)
    return (int(match.group("major")), int(match.group("minor")), int(match.group("patch")), pre_parts, raw)


def _compare_versions(left: str, right: str) -> int:
    left_parsed = _parse_semver(left)
    right_parsed = _parse_semver(right)
    if not left_parsed and not right_parsed:
        return 0
    if not left_parsed:
        return -1
    if not right_parsed:
        return 1
    left_key = left_parsed[:4]
    right_key = right_parsed[:4]
    if left_key < right_key:
        return -1
    if left_key > right_key:
        return 1
    return 0


def get_version_transition(current: str, target: str) -> Dict[str, str | bool]:
    current_parsed = _parse_semver(current)
    target_parsed = _parse_semver(target)
    if not current_parsed or not target_parsed:
        return {"allowed": False, "action": "blocked", "reason": "unrecognized_version"}

    cmp = _compare_versions(current, target)
    if cmp == 0:
        return {"allowed": False, "action": "current", "reason": "current"}

    current_major, current_minor = current_parsed[0], current_parsed[1]
    target_major, target_minor = target_parsed[0], target_parsed[1]
    if cmp < 0:
        is_major_upgrade = current_major != target_major
        return {
            "allowed": True,
            "action": "upgrade",
            "reason": "major_upgrade" if is_major_upgrade else "upgrade",
            "requires_confirm": is_major_upgrade,
        }

    if current_major != target_major:
        return {"allowed": False, "action": "blocked", "reason": "major_downgrade_blocked"}
    if current_major == 0 and current_minor != target_minor:
        return {"allowed": False, "action": "blocked", "reason": "pre1_minor_downgrade_blocked"}
    return {"allowed": True, "action": "downgrade", "reason": "downgrade"}


def check_for_updates(timeout: int = 60, branch: str | None = None) -> Dict[str, str]:
    selected_branch = str(branch or app_settings.get_updates_branch()).strip().lower() or "main"
    try:
        env = os.environ.copy()
        env["NODE_PLANE_SOURCE_DIR"] = _effective_source_root()
        proc = _run_cmd([_script_path("check_updates.sh"), "--branch", selected_branch], timeout=timeout, env=env)
        output = (proc.stdout or "").strip()
        if proc.stderr:
            output = f"{output}\n{proc.stderr.strip()}".strip()
        result = _parse_check_output(output, proc.returncode)
    except Exception as exc:
        result = {"status": "error", "message": str(exc)}
    result.setdefault("branch", selected_branch)
    result.setdefault("local_version", APP_SEMVER)
    result.setdefault("remote_version", result.get("local_version", APP_SEMVER))
    result.setdefault("local_label", APP_VERSION)
    result.setdefault("remote_label", result.get("remote_version", result.get("local_label", APP_VERSION)))
    result.setdefault("source_dir", _effective_source_root())
    result["checked_at"] = _utcnow_iso()
    app_settings.record_update_check(result)
    return result


def list_available_versions(branch: str | None = None, timeout: int = 60) -> Dict[str, object]:
    selected_branch = str(branch or app_settings.get_updates_branch()).strip().lower() or "main"
    try:
        env = os.environ.copy()
        env["NODE_PLANE_SOURCE_DIR"] = _effective_source_root()
        proc = _run_cmd([_script_path("check_updates.sh"), "--branch", selected_branch, "--list"], timeout=timeout, env=env)
        output = (proc.stdout or "").strip()
        if proc.stderr:
            output = f"{output}\n{proc.stderr.strip()}".strip()
        result = _parse_versions_output(output, proc.returncode)
    except Exception as exc:
        result = {"status": "error", "message": str(exc), "versions": []}
    result.setdefault("branch", selected_branch)
    result.setdefault("current_version", APP_SEMVER)
    current_version = str(result.get("current_version") or APP_SEMVER)
    versions = []
    for item in list(result.get("versions") or []):
        version = str(item.get("version") or "")
        ref = str(item.get("ref") or version)
        transition = get_version_transition(current_version, version)
        versions.append(
            {
                "version": version,
                "ref": ref,
                "kind": str(item.get("kind") or "tag"),
                "action": str(transition.get("action") or "blocked"),
                "allowed": bool(transition.get("allowed")),
                "reason": str(transition.get("reason") or ""),
                "requires_confirm": bool(transition.get("requires_confirm")),
            }
        )
    versions.sort(key=lambda item: _parse_semver(str(item["version"])) or (0, 0, 0, (0, 0), ""), reverse=True)
    result["versions"] = versions
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


def schedule_update(timeout: int = 30, branch: str | None = None, target_ref: str | None = None) -> Dict[str, str]:
    with _update_run_lock:
        state = refresh_update_run_state()
        if str(state.get("last_run_status") or "") == "running":
            return {"status": "running", "unit_name": str(state.get("last_run_unit") or "")}
        if not is_manual_update_supported():
            app_settings.record_update_run_finished("failed", _utcnow_iso(), "manual updates are only available in simple mode")
            state = app_settings.get_update_state()
            return {"status": "failed", "message": str(state.get("last_run_log_tail") or "")}
        source_root = _effective_source_root()
        selected_branch = str(branch or app_settings.get_updates_branch()).strip().lower() or "main"
        started_at = _utcnow_iso()
        unit_name = f"{UPDATE_UNIT_PREFIX}-{started_at.replace(':', '').replace('-', '').replace('T', '-').replace('Z', '').lower()}"
        try:
            backup_result = maybe_create_pre_action_backup("pre_update")
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
                "--branch",
                selected_branch,
            )
            if target_ref:
                cmd.extend(["--to", target_ref])
            proc = _run_cmd(cmd, cwd=source_root, timeout=timeout)
            output = ((proc.stdout or "").strip() + "\n" + (proc.stderr or "").strip()).strip()
            if backup_result.get("status") == "failed":
                output = (f"pre-update backup failed: {backup_result.get('message') or 'unknown error'}\n{output}").strip()
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
        result = check_for_updates(branch=app_settings.get_updates_branch())
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
    current_version = APP_SEMVER
    local_label = state.get("local_label", APP_VERSION)
    remote_label = state.get("remote_label", "")
    remote_version = state.get("remote_version", "") or _version_from_label(str(remote_label))
    if not remote_label:
        remote_label = remote_version or APP_VERSION
    update_available = state.get("update_available", "0") == "1"
    last_status = state.get("last_status", "never")
    if remote_version and _compare_versions(remote_version, current_version) <= 0:
        update_available = False
        if last_status == "available":
            last_status = "up_to_date"
    return {
        "branch": state.get("branch", app_settings.get_updates_branch()),
        "install_mode": detect_install_mode(),
        "current_version": current_version,
        "current_label": APP_VERSION,
        "source_dir": _effective_source_root(),
        "update_supported": is_manual_update_supported(),
        "auto_check_enabled": app_settings.is_updates_auto_check_enabled(),
        "last_checked_at": state.get("last_checked_at", ""),
        "last_status": last_status,
        "update_available": update_available,
        "local_version": state.get("local_version", "") or _version_from_label(str(local_label)) or current_version,
        "remote_version": remote_version,
        "local_label": local_label,
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
