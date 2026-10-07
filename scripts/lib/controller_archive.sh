#!/usr/bin/env bash
# Shared archive staging; never changes the active release or runtime secrets.
CONTROLLER_STAGE_DIR=""
CONTROLLER_REF=""
CONTROLLER_VERSION=""
CONTROLLER_COMMIT=""

prepare_controller_archive() {
  local branch="$1" ref="${2:-}" reuse="${3:-0}" temporary packaged_version=""
  temporary="$(mktemp -d)"
  if [[ -f "${REPO_ROOT}/VERSION" ]]; then packaged_version="$(tr -d '\r\n' < "${REPO_ROOT}/VERSION")"; fi
  if [[ "$reuse" == 1 && -f "${REPO_ROOT}/CONTROLLER_PACKAGE.json" ]] && \
      [[ -z "$ref" || "${ref#v}" == "$packaged_version" ]] && \
      [[ ( "$branch" == dev && "$packaged_version" == *-alpha.* ) || ( "$branch" == main && "$packaged_version" != *-alpha.* ) ]]; then
    # The workstation has already downloaded and verified this package.
    if ! python3 "${SCRIPT_DIR}/controller_release.py" copy --root "$REPO_ROOT" --destination "${temporary}/release"; then
      rm -rf "$temporary"
      return 1
    fi
  else
    if ! python3 "${SCRIPT_DIR}/controller_release.py" download --branch "$branch" --ref "$ref" --destination "${temporary}/release"; then
      rm -rf "$temporary"
      return 1
    fi
  fi
  CONTROLLER_STAGE_DIR="${temporary}/release"
  CONTROLLER_VERSION="$(tr -d '\r\n' < "${CONTROLLER_STAGE_DIR}/VERSION")"
  CONTROLLER_COMMIT="$(tr -d '\r\n' < "${CONTROLLER_STAGE_DIR}/BUILD_COMMIT")"
  CONTROLLER_REF="v${CONTROLLER_VERSION}"
}

cleanup_controller_archive() {
  if [[ -n "$CONTROLLER_STAGE_DIR" ]]; then
    rm -rf "$(dirname "$CONTROLLER_STAGE_DIR")"
  fi
}
