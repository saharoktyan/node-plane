"""Owned systemd installation resources, never a host-wide cleanup.

Installation paths come from an installer-written manifest, not an HTTP body.
The independent uninstall child is copied outside the checkout before launch.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from uuid import UUID

from .authorization import AccessDenied

UNITS = (
    "node-plane-telegram.service",
    "node-plane-backend-worker.timer",
    "node-plane-backend-worker.service",
    "node-plane-backend.service",
    "node-plane-driver.service",
    "node-plane.service",
)
FORBIDDEN = {
    "/",
    "/root",
    "/home",
    "/opt",
    "/usr",
    "/usr/local",
    "/var",
    "/var/lib",
    "/var/log",
    "/etc",
    "/tmp",
    "/run",
    "/bin",
    "/sbin",
}


def owned_path(value):
    path = Path(value)
    if (
        not path.is_absolute()
        or str(path) in FORBIDDEN
        or path.parent == Path("/home")
        or len(path.parts) < 3
        or path.is_symlink()
        or path.resolve() != path
    ):
        raise AccessDenied("unsafe_installation_path", 409)
    return path


class InstallationMaintenance:
    def __init__(self, staging_root="/run", units_root="/etc/systemd/system"):
        self.staging_root = Path(staging_root)
        self.units_root = Path(units_root)

    def deployment(self):
        if (
            os.geteuid() != 0
            or not shutil.which("systemctl")
            or not shutil.which("systemd-run")
        ):
            raise AccessDenied("system_cleanup_unsupported", 409)
        base = owned_path(os.environ.get("NODE_PLANE_BASE_DIR", "/opt/node-plane"))
        shared = owned_path(
            os.environ.get("NODE_PLANE_SHARED_DIR", str(base / "shared"))
        )
        marker = base / ".node-plane-installation.json"
        if marker.is_symlink() or not marker.is_file():
            raise AccessDenied("installation_manifest_required", 409)
        try:
            record = json.loads(marker.read_text())
        except (OSError, ValueError):
            raise AccessDenied("installation_manifest_required", 409) from None
        if record.get("format") != "node-plane-systemd-v1" or (
            record.get("base_dir"),
            record.get("shared_dir"),
        ) != (str(base), str(shared)):
            raise AccessDenied("installation_manifest_mismatch", 409)
        # The executing checkout must be owned by this installation.
        app = Path(
            os.environ.get("NODE_PLANE_APP_DIR", str(base / "current"))
        ).resolve()
        if not app.is_relative_to(base) or app == base:
            raise AccessDenied("installation_manifest_mismatch", 409)
        postgres = record.get("postgres_container")
        if postgres and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", postgres):
            raise AccessDenied("installation_manifest_mismatch", 409)
        return {
            "base_dir": str(base),
            "shared_dir": str(shared),
            "postgres_container": postgres,
            "units": list(UNITS),
        }

    def clear_local(self, expected, backup_id):
        if self.deployment() != expected:
            raise AccessDenied("installation_manifest_mismatch", 409)
        shared = Path(expected["shared_dir"])
        for candidate in (
            shared / "ssh",
            shared / "driver-agent-tls",
            shared / "backups",
            shared / "backups/backend",
            shared / ".env",
        ):
            if candidate.is_symlink():
                raise AccessDenied("unsafe_installation_path", 409)
        if not (shared / ".env").is_file():
            raise AccessDenied("installation_manifest_mismatch", 409)
        # The reset keeps one recovery snapshot, the active env and installation.
        for directory in ("ssh", "driver-agent-tls"):
            path = shared / directory
            if path.is_symlink():
                raise AccessDenied("unsafe_installation_path", 409)
            if path.exists():
                shutil.rmtree(path)
        backups = shared / "backups"
        if backups.is_symlink():
            raise AccessDenied("unsafe_installation_path", 409)
        keep = backups / "backend" / (backup_id + ".json")
        if backups.exists():
            for path in sorted(
                backups.rglob("*"), key=lambda p: len(p.parts), reverse=True
            ):
                if path == keep or path == keep.parent:
                    continue
                if path.is_symlink() or path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
        env = shared / ".env"
        if env.is_symlink():
            raise AccessDenied("unsafe_installation_path", 409)
        text = env.read_text()
        text = (
            "\n".join(
                line
                for line in text.splitlines()
                if not line.startswith("NODE_AGENT_TARGETS=")
            )
            + "\nNODE_AGENT_TARGETS=\n"
        )
        fd, temporary = tempfile.mkstemp(dir=shared, prefix=".reset-env-")
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, env)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        subprocess.run(
            ["systemctl", "restart", "node-plane-driver.service"],
            check=True,
            capture_output=True,
            timeout=30,
        )

    def prepare_uninstall(self, expected, job_id):
        if self.deployment() != expected:
            raise AccessDenied("installation_manifest_mismatch", 409)
        # Do not delete someone else's container just because its name matches.
        postgres = expected["postgres_container"]
        if postgres:
            result = subprocess.run(
                ["docker", "inspect", postgres],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            record = json.loads(result.stdout)
            data = str(Path(expected["shared_dir"]) / "postgres")
            if len(record) != 1 or not any(
                m.get("Source") == data
                and m.get("Destination") == "/var/lib/postgresql/data"
                for m in record[0]["Mounts"]
            ):
                raise AccessDenied("postgres_ownership_unverified", 409)
        base, shared = Path(expected["base_dir"]), Path(expected["shared_dir"])
        # Refuse to remove unit files that were repurposed after installation.
        for unit in UNITS:
            path = self.units_root / unit
            if path.is_symlink():
                raise AccessDenied("installation_manifest_mismatch", 409)
            if path.exists() and unit == "node-plane-backend-worker.timer":
                if "Unit=node-plane-backend-worker.service" not in path.read_text():
                    raise AccessDenied("installation_manifest_mismatch", 409)
            elif path.exists():
                text = path.read_text()
                if (
                    str(base / "current") not in text
                    or str(shared / ".env") not in text
                ):
                    raise AccessDenied("installation_manifest_mismatch", 409)
        job_id = str(UUID(job_id))
        directory = self.staging_root / ("node-plane-uninstall-" + job_id)
        if directory.is_symlink() or (directory / "remove.sh").is_symlink():
            raise AccessDenied("unsafe_installation_path", 409)
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
        script = directory / "remove.sh"
        lines = ["#!/usr/bin/env bash", "set -euo pipefail", "sleep 5"]
        for unit in UNITS:
            # Timer and worker must be stopped before the API/driver.
            lines.append(
                "systemctl disable --now "
                + shlex.quote(unit)
                + " >/dev/null 2>&1 || { "
                "systemctl cat "
                + shlex.quote(unit)
                + " >/dev/null 2>&1 && exit 1 || true; }"
            )
        if postgres:
            lines.append("docker rm -f " + shlex.quote(postgres) + " >/dev/null")
        for unit in UNITS:
            lines.append("rm -f -- " + shlex.quote("/etc/systemd/system/" + unit))
        lines += [
            "rm -f -- /usr/local/bin/node-plane-driver",
            "systemctl daemon-reload",
        ]
        targets = [shared] if not shared.is_relative_to(base) else []
        targets.append(base)
        for path in targets:
            lines.append("rm -rf -- " + shlex.quote(str(path)))
        lines += [
            'echo "Node Plane installation removed"',
            "rm -rf -- " + shlex.quote(str(directory)),
        ]
        script.write_text("\n".join(lines) + "\n")
        script.chmod(0o700)
        return {"script": str(script), "unit": "node-plane-uninstall-" + job_id}

    def launch_uninstall(self, plan):
        subprocess.run(
            [
                "systemd-run",
                "--unit",
                plan["unit"],
                "--collect",
                "--no-block",
                "--property=Type=oneshot",
                "/bin/bash",
                plan["script"],
            ],
            check=True,
            capture_output=True,
            timeout=10,
        )
        return {"unit": plan["unit"]}

    def discard_uninstall(self, plan):
        directory = self.staging_root / plan["unit"]
        suffix = plan["unit"].removeprefix("node-plane-uninstall-")
        if (
            plan["unit"] != "node-plane-uninstall-" + str(UUID(suffix))
            or directory.is_symlink()
        ):
            raise AccessDenied("unsafe_installation_path", 409)
        if Path(plan["script"]) != directory / "remove.sh":
            raise AccessDenied("unsafe_installation_path", 409)
        if directory.exists():
            shutil.rmtree(directory)
