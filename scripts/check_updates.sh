#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SOURCE_DIR="${NODE_PLANE_SOURCE_DIR:-$REPO_ROOT}"
INSTALLED_APP_DIR="${NODE_PLANE_APP_DIR:-${NODE_PLANE_BASE_DIR:-$REPO_ROOT}}"
DEFAULT_BRANCH="${NODE_PLANE_UPDATE_BRANCH:-main}"
BRANCH=""
LIST_MODE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --branch)
      BRANCH="${2:-}"
      shift 2
      ;;
    --branch=*)
      BRANCH="${1#*=}"
      shift
      ;;
    --list)
      LIST_MODE=1
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

read_version_file() {
  local file="$1"
  if [[ -f "$file" ]]; then
    tr -d '\n' < "$file"
  else
    echo "0.1.0"
  fi
}

read_installed_version() {
  read_version_file "${INSTALLED_APP_DIR}/VERSION"
}

read_installed_commit() {
  if [[ -f "${INSTALLED_APP_DIR}/BUILD_COMMIT" ]]; then
    tr -d '\n' < "${INSTALLED_APP_DIR}/BUILD_COMMIT"
  elif git -C "${INSTALLED_APP_DIR}" rev-parse --short HEAD >/dev/null 2>&1; then
    git -C "${INSTALLED_APP_DIR}" rev-parse --short HEAD
  else
    echo "unknown"
  fi
}

emit_error() {
  local message="$1"
  echo "CHECK_UPDATES|error"
  echo "message: ${message}"
  echo "source_dir: ${SOURCE_DIR}"
  exit 1
}

stable_tag_regex='^v?[0-9]+\.[0-9]+\.[0-9]+$'
alpha_tag_regex='^v?[0-9]+\.[0-9]+\.[0-9]+-alpha\.[0-9]+$'

latest_tag_for_branch() {
  local branch="$1"
  local regex
  if [[ "$branch" == "main" ]]; then
    regex="$stable_tag_regex"
  else
    regex="$alpha_tag_regex"
  fi
  while IFS= read -r tag; do
    [[ -z "$tag" ]] && continue
    if [[ "$tag" =~ $regex ]]; then
      echo "$tag"
      return 0
    fi
  done < <(git tag --merged "origin/${branch}" --sort=-version:refname)
  return 1
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

if [[ -z "$BRANCH" ]]; then
  upstream_branch="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
  if [[ -n "$upstream_branch" ]]; then
    BRANCH="${upstream_branch#*/}"
  else
    BRANCH="$DEFAULT_BRANCH"
  fi
fi

if ! git fetch --quiet --tags origin; then
  emit_error "git fetch failed"
fi

BRANCH_REF="origin/${BRANCH}"
if ! git rev-parse --verify "${BRANCH_REF}^{commit}" >/dev/null 2>&1; then
  emit_error "branch '${BRANCH}' not found on origin"
fi

LATEST_TAG="$(latest_tag_for_branch "$BRANCH" || true)"
UPSTREAM_REF="${LATEST_TAG:-$BRANCH_REF}"

LOCAL_COMMIT="$(read_installed_commit)"
REMOTE_COMMIT="$(git rev-parse --short "${UPSTREAM_REF}")"
LOCAL_VERSION="$(read_installed_version)"
REMOTE_VERSION="$(git show "${UPSTREAM_REF}:VERSION" 2>/dev/null | tr -d '\n' || true)"
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

if [[ $LIST_MODE -eq 1 ]]; then
  echo "LIST_VERSIONS|ok"
  echo "branch: ${BRANCH}"
  echo "source_dir: ${SOURCE_DIR}"
  echo "current_version: ${LOCAL_VERSION}"
  if [[ "$BRANCH" == "main" ]]; then
    tag_regex="$stable_tag_regex"
  else
    tag_regex="$alpha_tag_regex"
  fi
  while IFS= read -r tag; do
    [[ -z "$tag" ]] && continue
    if [[ "$tag" =~ $tag_regex ]]; then
      echo "version_item: ${tag#v}|${tag}|tag"
    fi
  done < <(git tag --merged "${BRANCH_REF}" --sort=-version:refname)
  exit 0
fi

if [[ "$LOCAL_COMMIT" == "$REMOTE_COMMIT" ]]; then
  echo "CHECK_UPDATES|up_to_date"
else
  echo "CHECK_UPDATES|available"
fi
echo "branch: ${BRANCH}"
echo "source_dir: ${SOURCE_DIR}"
echo "upstream_ref: ${UPSTREAM_REF}"
echo "local_commit: ${LOCAL_COMMIT}"
echo "remote_commit: ${REMOTE_COMMIT}"
echo "local_version: ${LOCAL_VERSION}"
echo "remote_version: ${REMOTE_VERSION}"
echo "local_label: ${LOCAL_LABEL}"
echo "remote_label: ${REMOTE_LABEL}"
