"""Provision the driver transport before a new SSH node can serve runtime RPCs."""

from __future__ import annotations

import os
import shutil
import subprocess

from config import APP_ROOT, NODE_DRIVER_BACKEND, SHARED_ROOT
from services.server_registry import get_server


def ensure_driver_agent_rollout_for_ssh(server_key: str) -> tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Server {server_key} not found"
    if server.transport != "ssh":
        return 0, ""
    script_path = f"{APP_ROOT}/scripts/setup_driver_agents.sh"
    if not os.path.isfile(script_path):
        return 1, f"missing rollout script: {script_path}"
    env = os.environ.copy()
    env["NODE_PLANE_APP_DIR"] = APP_ROOT
    env["NODE_PLANE_SHARED_DIR"] = SHARED_ROOT
    env.setdefault("NODE_PLANE_BIN_SOURCE", "build" if shutil.which("cargo") else "release")
    try:
        proc = subprocess.run(
            [script_path, "--strict", "--node-key", server_key],
            cwd=APP_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=900,
        )
    except Exception as exc:
        return 1, f"driver/agent rollout failed to start: {exc}"
    output = ((proc.stdout or "").strip() + "\n" + (proc.stderr or "").strip()).strip()
    if proc.returncode == 0 and NODE_DRIVER_BACKEND != "grpc":
        output += "\nRestart the bot to activate NODE_DRIVER_BACKEND=grpc from the updated environment."
    return proc.returncode, output or f"exit={proc.returncode}"
