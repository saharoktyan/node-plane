from __future__ import annotations

import os
import subprocess


def _candidate_shared_root() -> str:
    install_root = os.getenv("NODE_PLANE_BASE_DIR", "/opt/node-plane").strip() or "/opt/node-plane"
    app_root = os.getenv("NODE_PLANE_APP_DIR", install_root).strip() or install_root
    return os.getenv("NODE_PLANE_SHARED_DIR", app_root).strip() or app_root


def _load_runtime_env_file() -> None:
    env_path = os.path.join(_candidate_shared_root(), ".env")
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if not key:
                    continue
                os.environ.setdefault(key, value.strip())
    except OSError:
        return


_load_runtime_env_file()


def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw.strip())


def _env_int_list(name: str) -> list[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    return [int(part.strip()) for part in raw.split(",") if part.strip()]


BOT_TOKEN = _env_str("BOT_TOKEN")
ADMIN_IDS = _env_int_list("ADMIN_IDS")

INSTALL_ROOT = _env_str("NODE_PLANE_BASE_DIR", "/opt/node-plane")
APP_ROOT = _env_str("NODE_PLANE_APP_DIR", INSTALL_ROOT)
SHARED_ROOT = _env_str("NODE_PLANE_SHARED_DIR", APP_ROOT)
SOURCE_ROOT = _env_str("NODE_PLANE_SOURCE_DIR", APP_ROOT)
INSTALL_MODE = _env_str("NODE_PLANE_INSTALL_MODE", "")
UPDATE_BRANCH = _env_str("NODE_PLANE_UPDATE_BRANCH", "main").lower()

# BASE_DIR is kept as an app-root alias for compatibility with the current codebase.
BASE_DIR = APP_ROOT
APP_DIR = f"{APP_ROOT}/app"
DATA_DIR = f"{SHARED_ROOT}/data"
SSH_DIR = f"{SHARED_ROOT}/ssh"

DB_BACKEND = _env_str("DB_BACKEND", "postgres").lower()
POSTGRES_DSN = _env_str("POSTGRES_DSN")

if DB_BACKEND != "postgres":
    raise ValueError(f"Unsupported DB_BACKEND: {DB_BACKEND}")

SSH_KEY = _env_str("SSH_KEY") or f"{SSH_DIR}/id_ed25519"
SSH_STRICT_HOST_KEY_CHECKING = _env_str("SSH_STRICT_HOST_KEY_CHECKING", "yes")
SSH_KNOWN_HOSTS_PATH = _env_str("SSH_KNOWN_HOSTS_PATH", f"{SSH_DIR}/known_hosts")

NODE_DRIVER_GRPC_TARGET = _env_str("NODE_DRIVER_GRPC_TARGET", "127.0.0.1:50051")
NODE_DRIVER_GRPC_TIMEOUT_SECONDS = max(1, _env_int("NODE_DRIVER_GRPC_TIMEOUT_SECONDS", 30))


def _git_version() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=APP_ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return out.strip()
    except Exception:
        return "unknown"


def _build_commit_file_value() -> str:
    try:
        with open(f"{APP_ROOT}/BUILD_COMMIT", "r", encoding="utf-8") as fh:
            value = fh.read().strip()
            return value or "unknown"
    except Exception:
        return "unknown"


def _version_file_value() -> str:
    try:
        with open(f"{APP_ROOT}/VERSION", "r", encoding="utf-8") as fh:
            value = fh.read().strip()
            return value or "0.1.0"
    except Exception:
        return "0.1.0"


APP_SEMVER = _env_str("APP_SEMVER", _version_file_value())
_DEFAULT_APP_COMMIT = _git_version()
if _DEFAULT_APP_COMMIT == "unknown":
    _DEFAULT_APP_COMMIT = _build_commit_file_value()
APP_COMMIT = _env_str("APP_COMMIT", _DEFAULT_APP_COMMIT)
APP_VERSION = _env_str("APP_VERSION", f"{APP_SEMVER} · {APP_COMMIT}" if APP_COMMIT != "unknown" else APP_SEMVER)
