#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

TARGET_RELEASE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --to)
      TARGET_RELEASE="${2:-}"
      shift 2
      ;;
    --to=*)
      TARGET_RELEASE="${1#*=}"
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage:
  scripts/rollback.sh --to <release-id>

Example:
  ./scripts/rollback.sh --to 0.1.0-95adaf1
EOF
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

read_env_value() {
  local key="$1"
  if [[ ! -f "${REPO_ROOT}/.env" ]]; then
    return 0
  fi
  sed -n "s/^${key}=//p" "${REPO_ROOT}/.env" | tail -n 1
}

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

need_cmd sudo

if [[ -z "$TARGET_RELEASE" ]]; then
  echo "--to <release-id> is required." >&2
  exit 1
fi

BASE_DIR="$(read_env_value NODE_PLANE_BASE_DIR)"
APP_DIR="$(read_env_value NODE_PLANE_APP_DIR)"
if [[ -z "$BASE_DIR" ]]; then
  BASE_DIR="$REPO_ROOT"
fi
if [[ -z "$APP_DIR" ]]; then
  APP_DIR="${BASE_DIR}/current"
fi

RELEASE_DIR="${BASE_DIR}/releases/${TARGET_RELEASE}"
if [[ ! -d "$RELEASE_DIR" ]]; then
  echo "Release not found: ${RELEASE_DIR}" >&2
  exit 1
fi

echo "Rolling back to release:"
echo "  ${RELEASE_DIR}"

ln -sfn "$RELEASE_DIR" "$APP_DIR"
sudo systemctl daemon-reload
sudo systemctl restart node-plane
sudo systemctl status node-plane --no-pager || true
