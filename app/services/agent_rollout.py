"""Provision the driver transport before a new SSH node can serve runtime RPCs."""

from __future__ import annotations

import os
import subprocess

from config import APP_ROOT, NODE_DRIVER_BACKEND, SHARED_ROOT
from services.app_settings import set_agent_rollout_pending
from services.server_registry import get_server


def ensure_driver_agent_rollout_for_ssh(
    server_key: str, *, skip_if_connected: bool = False, install_rust: bool = False
) -> tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Server {server_key} not found"
    if server.transport != "ssh":
        return 0, ""
    if skip_if_connected and NODE_DRIVER_BACKEND == "grpc":
        # Bootstrap already reached this node through its agent. Rollout is a
        # separate maintenance action, not a second bootstrap requirement.
        set_agent_rollout_pending(server_key, False)
        return 0, ""
    script_path = f"{APP_ROOT}/scripts/setup_driver_agents.sh"
    if not os.path.isfile(script_path):
        set_agent_rollout_pending(server_key, True)
        return 1, f"missing rollout script: {script_path}"
    env = os.environ.copy()
    env["NODE_PLANE_APP_DIR"] = APP_ROOT
    env["NODE_PLANE_SHARED_DIR"] = SHARED_ROOT
    env.setdefault("NODE_PLANE_BIN_SOURCE", "auto")
    env["NODE_PLANE_INSTALL_RUST"] = "yes" if install_rust else "no"
    try:
        proc = subprocess.run(
            [script_path, "--strict", "--node-key", server_key],
            cwd=APP_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except Exception as exc:
        set_agent_rollout_pending(server_key, True)
        return 1, f"driver/agent rollout failed to start: {exc}"
    output = ((proc.stdout or "").strip() + "\n" + (proc.stderr or "").strip()).strip()
    set_agent_rollout_pending(server_key, proc.returncode != 0)
    if proc.returncode == 0 and NODE_DRIVER_BACKEND != "grpc":
        output += "\nRestart the bot to activate NODE_DRIVER_BACKEND=grpc from the updated environment."
    return proc.returncode, output or f"exit={proc.returncode}"
