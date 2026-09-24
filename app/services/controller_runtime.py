"""Commands used only for maintaining the controller installation itself."""

from __future__ import annotations

import os
import subprocess
from typing import Tuple


def is_running_in_container() -> bool:
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup", "r", encoding="utf-8") as stream:
            return any(token in stream.read() for token in ("docker", "containerd", "kubepods"))
    except OSError:
        return False


def run_local_command(command: str, timeout: int = 60) -> Tuple[int, str]:
    try:
        result = subprocess.run(
            ["/usr/bin/bash", "-lc", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return result.returncode, ((result.stdout or "") + (result.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"
    except Exception as exc:
        return 1, f"Exception: {exc}"
