from __future__ import annotations

import os
import subprocess
from typing import Any, Dict, List

from config import APP_ROOT, INSTALL_MODE, INSTALL_ROOT, SOURCE_ROOT


DEFAULT_KEEP_COUNT = 2


def _script_path() -> str:
    candidates = [
        os.path.join(SOURCE_ROOT, "scripts", "cleanup_releases.sh"),
        os.path.join(APP_ROOT, "scripts", "cleanup_releases.sh"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return candidates[0]


def _release_dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            full = os.path.join(root, name)
            try:
                total += os.path.getsize(full)
            except OSError:
                continue
    return total


def _release_dirs() -> List[str]:
    releases_dir = os.path.join(INSTALL_ROOT, "releases")
    if not os.path.isdir(releases_dir):
        return []
    items: List[str] = []
    for name in os.listdir(releases_dir):
        path = os.path.join(releases_dir, name)
        if os.path.isdir(path):
            items.append(path)
    items.sort(key=lambda path: os.path.getmtime(path), reverse=True)
    return items


def get_release_cleanup_overview(keep_count: int = DEFAULT_KEEP_COUNT) -> Dict[str, Any]:
    install_mode = str(INSTALL_MODE or "").strip().lower() or "simple"
    releases_dir = os.path.join(INSTALL_ROOT, "releases")
    current_target = os.path.realpath(APP_ROOT) if APP_ROOT else ""
    releases = _release_dirs()
    keep: List[str] = list(releases[:keep_count])
    if current_target and current_target not in keep and current_target in releases:
        keep.append(current_target)
    removable = [path for path in releases if path not in keep]
    removable_size = sum(_release_dir_size(path) for path in removable)
    total_size = sum(_release_dir_size(path) for path in releases)
    return {
        "supported": install_mode == "simple",
        "install_mode": install_mode,
        "releases_dir": releases_dir,
        "script_path": _script_path(),
        "keep_count": keep_count,
        "total_releases": len(releases),
        "kept_releases": len(keep),
        "removable_releases": len(removable),
        "total_size_bytes": total_size,
        "removable_size_bytes": removable_size,
        "current_target": current_target,
        "releases": releases,
        "removable": removable,
    }


def run_release_cleanup(keep_count: int = DEFAULT_KEEP_COUNT) -> Dict[str, Any]:
    overview = get_release_cleanup_overview(keep_count=keep_count)
    if not overview.get("supported"):
        return {"status": "unsupported", "message": "release cleanup is available only in simple mode", **overview}
    if int(overview.get("removable_releases") or 0) <= 0:
        return {"status": "noop", "message": "nothing to remove", **overview}
    script_path = str(overview.get("script_path") or "")
    if not os.path.isfile(script_path):
        return {"status": "failed", "message": f"cleanup script not found: {script_path}", **overview}
    env = os.environ.copy()
    env["NODE_PLANE_BASE_DIR"] = INSTALL_ROOT
    env["NODE_PLANE_APP_DIR"] = APP_ROOT
    env["NODE_PLANE_SOURCE_DIR"] = SOURCE_ROOT
    proc = subprocess.run(
        [script_path, "--keep", str(keep_count)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    refreshed = get_release_cleanup_overview(keep_count=keep_count)
    if proc.returncode != 0:
        return {"status": "failed", "message": output or f"cleanup failed with rc={proc.returncode}", **refreshed}
    return {
        "status": "success",
        "message": output or "cleanup complete",
        "removed": int(overview.get("removable_releases") or 0) - int(refreshed.get("removable_releases") or 0),
        **refreshed,
    }
