#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

KEEP_COUNT=2
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep)
      KEEP_COUNT="${2:-}"
      shift 2
      ;;
    --keep=*)
      KEEP_COUNT="${1#*=}"
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage:
  scripts/cleanup_releases.sh [--keep 2] [--dry-run]

By default, keeps the two most recent releases and also preserves the current release target.
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

BASE_DIR="$(read_env_value NODE_PLANE_BASE_DIR)"
APP_DIR="$(read_env_value NODE_PLANE_APP_DIR)"
if [[ -z "$BASE_DIR" ]]; then
  BASE_DIR="$REPO_ROOT"
fi
if [[ -z "$APP_DIR" ]]; then
  APP_DIR="${BASE_DIR}/current"
fi

RELEASES_DIR="${BASE_DIR}/releases"
CURRENT_TARGET="$(readlink -f "$APP_DIR" 2>/dev/null || true)"

if [[ ! -d "$RELEASES_DIR" ]]; then
  echo "No releases directory found at ${RELEASES_DIR}"
  exit 0
fi

mapfile -t RELEASES < <(find "$RELEASES_DIR" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -rn | awk '{print $2}')

if [[ ${#RELEASES[@]} -le KEEP_COUNT ]]; then
  echo "Nothing to clean. Release count: ${#RELEASES[@]}"
  exit 0
fi

declare -A KEEP=()
index=0
for release in "${RELEASES[@]}"; do
  if (( index < KEEP_COUNT )); then
    KEEP["$release"]=1
  fi
  index=$((index + 1))
done
if [[ -n "$CURRENT_TARGET" ]]; then
  KEEP["$CURRENT_TARGET"]=1
fi

echo "Keeping releases:"
for release in "${!KEEP[@]}"; do
  echo "  $release"
done

echo
echo "Removing releases:"
removed=0
for release in "${RELEASES[@]}"; do
  if [[ -n "${KEEP[$release]:-}" ]]; then
    continue
  fi
  echo "  $release"
  removed=$((removed + 1))
  if [[ $DRY_RUN -eq 0 ]]; then
    rm -rf "$release"
  fi
done

if [[ $removed -eq 0 ]]; then
  echo "Nothing to remove."
elif [[ $DRY_RUN -eq 1 ]]; then
  echo "Dry run only. No releases were deleted."
else
  echo "Cleanup complete."
fi
