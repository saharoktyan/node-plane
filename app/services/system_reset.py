from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import List, Tuple

from config import BASE_DIR, INSTALL_MODE, SHARED_ROOT, SOURCE_ROOT, SSH_DIR, SQLITE_DB_PATH
from db.schema import ensure_schema
from db.sqlite_db import SQLiteDB
from services.backups import clear_backup_storage, maybe_create_pre_action_backup
from services.server_bootstrap import full_cleanup_server
from services.server_registry import list_servers


_db = SQLiteDB(SQLITE_DB_PATH)


def _wipe_local_state() -> None:
    with _db.transaction() as conn:
        ensure_schema(conn)
        for table in (
            "profile_server_state",
            "awg_server_configs",
            "xray_transports",
            "xray_profiles",
            "profile_access_methods",
            "profile_state",
            "traffic_samples",
            "telegram_users",
            "profiles",
            "servers",
            "schema_meta",
        ):
            conn.execute(f"DELETE FROM {table}")


def _clear_local_ssh_material() -> str:
    if not os.path.isdir(SSH_DIR):
        os.makedirs(SSH_DIR, exist_ok=True)
        return "local SSH directory reset"
    for entry in os.listdir(SSH_DIR):
        path = os.path.join(SSH_DIR, entry)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.remove(path)
            except FileNotFoundError:
                continue
    os.makedirs(SSH_DIR, exist_ok=True)
    return "local SSH material removed"


def _uninstall_targets() -> List[str]:
    values = [str(BASE_DIR or "").strip(), str(SHARED_ROOT or "").strip(), str(SOURCE_ROOT or "").strip()]
    targets: List[str] = []
    for path in values:
        if not path or path in {"/", "/root", "/home"}:
            continue
        normalized = os.path.abspath(path)
        if normalized in {"/", "/root", "/home"}:
            continue
        if normalized not in targets:
            targets.append(normalized)
    return targets


def _shell_quote(value: str) -> str:
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def _systemctl_prefix() -> str:
    return "" if os.geteuid() == 0 else "sudo -n "


def _read_env_var_from_shared(key: str) -> str:
    env_path = os.path.join(SHARED_ROOT, ".env")
    if not os.path.isfile(env_path):
        return ""
    try:
        with open(env_path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                if name.strip() == key:
                    return value.strip()
    except OSError:
        return ""
    return ""


def schedule_full_uninstall() -> Tuple[int, str]:
    targets = _uninstall_targets()
    if not targets:
        return 1, "No safe uninstall paths were resolved."

    prefix = _systemctl_prefix()
    pid = os.getpid()
    image_repo = _read_env_var_from_shared("NODE_PLANE_IMAGE_REPO") or "node-plane"
    image_tag = _read_env_var_from_shared("NODE_PLANE_IMAGE_TAG") or "local"
    image_ref = f"{image_repo}:{image_tag}" if image_repo and image_tag else ""
    script_body = [
        "#!/usr/bin/env bash",
        "set -eu",
        "sleep 3",
        f"{prefix}systemctl stop node-plane >/dev/null 2>&1 || true",
        f"{prefix}systemctl disable node-plane >/dev/null 2>&1 || true",
        f"{prefix}rm -f /etc/systemd/system/node-plane.service >/dev/null 2>&1 || true",
        f"{prefix}systemctl daemon-reload >/dev/null 2>&1 || true",
        "docker rm -f node-plane >/dev/null 2>&1 || true",
    ]
    if image_ref:
        script_body.append(f"docker rmi -f {_shell_quote(image_ref)} >/dev/null 2>&1 || true")
    script_body.extend(
        [
            "docker image prune -f >/dev/null 2>&1 || true",
            "docker system prune -f >/dev/null 2>&1 || true",
        "kill " + str(pid) + " >/dev/null 2>&1 || true",
        ]
    )
    for path in targets:
        script_body.append(f"rm -rf -- {_shell_quote(path)} >/dev/null 2>&1 || true")
    script_body.append('rm -f -- "$0" >/dev/null 2>&1 || true')
    fd, script_path = tempfile.mkstemp(prefix="node-plane-uninstall-", suffix=".sh")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(script_body) + "\n")
        os.chmod(script_path, 0o700)
        subprocess.Popen(
            ["/bin/sh", script_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        try:
            os.remove(script_path)
        except OSError:
            pass
        return 1, f"Failed to schedule Node Plane removal: {exc}"

    lines = ["Node Plane removal scheduled.", "", "Targets:"]
    lines.extend(f"• {path}" for path in targets)
    lines.extend(
        [
            "",
            "The bot process will stop in a few seconds.",
            "The local service/container and installation paths will be removed.",
        ]
    )
    return 0, "\n".join(lines)


def run_full_remove(cleanup_nodes: bool = False) -> Tuple[int, str]:
    if cleanup_nodes:
        failures: List[str] = []
        completed: List[str] = []
        for server in list_servers(include_disabled=True):
            rc, out = full_cleanup_server(
                server.key,
                remove_ssh_key=(server.transport == "ssh"),
            )
            if rc != 0:
                failures.append(f"{server.key}: {(out or '').strip()[:400]}")
            else:
                completed.append(server.key)
        if failures:
            lines = ["Node cleanup failed.", ""]
            if completed:
                lines.append("Completed:")
                lines.extend(f"• {key}" for key in completed)
                lines.append("")
            lines.append("Errors:")
            lines.extend(f"• {item}" for item in failures[:10])
            lines.append("")
            lines.append("Node Plane removal was not scheduled.")
            return 1, "\n".join(lines)

    rc, out = schedule_full_uninstall()
    if rc != 0 or not cleanup_nodes:
        return rc, out
    return 0, out + "\n\n• managed runtimes cleaned up on registered nodes\n• bot SSH key removal requested for SSH nodes"


def _schedule_portable_container_teardown() -> Tuple[bool, str]:
    if str(INSTALL_MODE or "").strip().lower() != "portable":
        return False, "portable teardown not requested"
    docker_sock = "/var/run/docker.sock"
    if not (os.path.exists(docker_sock) and os.access(docker_sock, os.R_OK | os.W_OK)):
        return False, "portable control-plane container must be removed manually on the host"
    try:
        subprocess.Popen(
            [
                "/bin/sh",
                "-c",
                "sleep 5; docker rm -f node-plane >/tmp/node-plane-destroy.log 2>&1 || true",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True, "Node Plane container teardown scheduled"
    except Exception as exc:
        return False, f"portable control-plane container was not scheduled for teardown: {exc}"


def run_factory_reset(cleanup_nodes: bool = False, stop_local_runtime: bool = False) -> Tuple[int, str]:
    backup_result = maybe_create_pre_action_backup("pre_reset")
    if cleanup_nodes:
        failures: List[str] = []
        completed: List[str] = []
        for server in list_servers(include_disabled=True):
            rc, out = full_cleanup_server(
                server.key,
                remove_ssh_key=(server.transport == "ssh"),
            )
            if rc != 0:
                failures.append(f"{server.key}: {(out or '').strip()[:400]}")
            else:
                completed.append(server.key)
        if failures:
            lines = ["Node cleanup failed.", ""]
            if completed:
                lines.append("Completed:")
                lines.extend(f"• {key}" for key in completed)
                lines.append("")
            lines.append("Errors:")
            lines.extend(f"• {item}" for item in failures[:10])
            lines.append("")
            lines.append("Local state was not removed.")
            return 1, "\n".join(lines)

    _wipe_local_state()
    ssh_line = _clear_local_ssh_material()
    backup_clear_result = clear_backup_storage()

    summary = ["Node Plane state removed.", "• local database state cleared", f"• {ssh_line}", f"• backups removed: {int(backup_clear_result.get('removed') or 0)}"]
    if backup_result.get("status") == "failed":
        summary.append(f"• pre-reset backup failed before cleanup: {backup_result.get('message') or 'unknown error'}")
    if cleanup_nodes:
        summary.append("• managed runtimes cleaned up on registered nodes")
        summary.append("• bot SSH key removal requested for SSH nodes")
    if stop_local_runtime:
        ok, message = _schedule_portable_container_teardown()
        summary.append(f"• {message}")
    return 0, "\n".join(summary)
