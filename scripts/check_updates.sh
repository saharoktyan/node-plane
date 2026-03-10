#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SOURCE_DIR="${NODE_PLANE_SOURCE_DIR:-$REPO_ROOT}"

read_version_file() {
  local file="$1"
  if [[ -f "$file" ]]; then
    tr -d '\n' < "$file"
  else
    echo "0.1.0"
  fi
}

emit_error() {
  local message="$1"
  echo "CHECK_UPDATES|error"
  echo "message: ${message}"
  echo "source_dir: ${SOURCE_DIR}"
  exit 1
}

if [[ ! -d "$SOURCE_DIR" ]]; then
  emit_error "source checkout not found"
fi

cd "$SOURCE_DIR"

if ! command -v git >/dev/null 2>&1; then
  emit_error "git is not installed"
fi

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  emit_error "source checkout is not a git repository"
fi

if ! git rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
  emit_error "git upstream is not configured"
fi

if ! git fetch --quiet; then
  emit_error "git fetch failed"
fi

LOCAL_COMMIT="$(git rev-parse --short HEAD)"
UPSTREAM_REF="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}')"
REMOTE_COMMIT="$(git rev-parse --short '@{u}')"
LOCAL_VERSION="$(read_version_file VERSION)"
REMOTE_VERSION="$(git show '@{u}:VERSION' 2>/dev/null | tr -d '\n' || true)"
if [[ -z "$REMOTE_VERSION" ]]; then
  REMOTE_VERSION="$LOCAL_VERSION"
fi

LOCAL_LABEL="${LOCAL_VERSION}"
REMOTE_LABEL="${REMOTE_VERSION}"
if [[ -n "$LOCAL_COMMIT" && "$LOCAL_COMMIT" != "unknown" ]]; then
  LOCAL_LABEL="${LOCAL_LABEL} · ${LOCAL_COMMIT}"
fi
if [[ -n "$REMOTE_COMMIT" && "$REMOTE_COMMIT" != "unknown" ]]; then
  REMOTE_LABEL="${REMOTE_LABEL} · ${REMOTE_COMMIT}"
fi

if [[ "$LOCAL_COMMIT" == "$REMOTE_COMMIT" ]]; then
  echo "CHECK_UPDATES|up_to_date"
else
  echo "CHECK_UPDATES|available"
fi
echo "source_dir: ${SOURCE_DIR}"
echo "upstream_ref: ${UPSTREAM_REF}"
echo "local_commit: ${LOCAL_COMMIT}"
echo "remote_commit: ${REMOTE_COMMIT}"
echo "local_version: ${LOCAL_VERSION}"
echo "remote_version: ${REMOTE_VERSION}"
echo "local_label: ${LOCAL_LABEL}"
echo "remote_label: ${REMOTE_LABEL}"
