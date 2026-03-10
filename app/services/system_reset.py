from __future__ import annotations

import os
import shutil
import subprocess
from typing import List, Tuple

from config import INSTALL_MODE, SSH_DIR, SQLITE_DB_PATH
from db.schema import ensure_schema
from db.sqlite_db import SQLiteDB
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

    summary = ["Node Plane state removed.", "• local database state cleared", f"• {ssh_line}"]
    if cleanup_nodes:
        summary.append("• managed runtimes cleaned up on registered nodes")
        summary.append("• bot SSH key removal requested for SSH nodes")
    if stop_local_runtime:
        ok, message = _schedule_portable_container_teardown()
        summary.append(f"• {message}")
    return 0, "\n".join(summary)
