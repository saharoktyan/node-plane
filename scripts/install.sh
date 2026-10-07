#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
source "${SCRIPT_DIR}/postgres_runtime.sh"
source "${SCRIPT_DIR}/python_runtime.sh"
source "${SCRIPT_DIR}/uv_runtime.sh"
source "${SCRIPT_DIR}/lib/install_progress.sh"
source "${SCRIPT_DIR}/lib/controller_archive.sh"
FROM_SOURCE=0

MODE="${MODE:-}"
NON_INTERACTIVE=0
AUTO_INSTALL_SYSTEMD=0
UPDATE_BRANCH="${NODE_PLANE_UPDATE_BRANCH:-}"
INSTALL_REF="${NODE_PLANE_INSTALL_REF:-}"
FORCE_REINSTALL=0
CURRENT_STEP="startup"
AUTO_SETUP_DRIVER_AGENTS_ON_INSTALL="${NODE_PLANE_AUTO_SETUP_DRIVER_AGENTS_ON_INSTALL:-1}"
PYTHON_BIN=""
CONFIG_ENV_FILE="${NODE_PLANE_INSTALL_ENV_FILE:-${REPO_ROOT}/.env}"
EXPLICIT_CONFIG_FILE=0
WORKSTATION_INSTALL_MARKER=""

set_step() {
  CURRENT_STEP="$1"
  install_progress_detail "$1"
}

on_error() {
  local exit_code="$1"
  echo >&2
  echo "Install failed during step: ${CURRENT_STEP}" >&2
  # Shell commands may contain DSNs or credentials. Never expose them through
  # the workstation protocol or its accompanying error output.
  if [[ "${NODE_PLANE_INSTALL_EVENTS:-0}" != "1" ]]; then
    echo "Failing command: ${BASH_COMMAND}" >&2
  fi
  echo "Exit code: ${exit_code}" >&2
}

trap 'on_error $?' ERR
trap 'install_exit_status=$?; if [[ $install_exit_status -ne 0 ]]; then install_progress_fail; fi; cleanup_controller_archive' EXIT

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      MODE="${2:-}"
      shift 2
      ;;
    --mode=*)
      MODE="${1#*=}"
      shift
      ;;
    --non-interactive)
      NON_INTERACTIVE=1
      shift
      ;;
    --progress-json)
      NODE_PLANE_INSTALL_EVENTS=1
      shift
      ;;
    --env-file)
      if [[ $# -lt 2 || -z "$2" ]]; then
        echo "--env-file requires a configuration file path." >&2
        exit 1
      fi
      CONFIG_ENV_FILE="$2"
      EXPLICIT_CONFIG_FILE=1
      shift 2
      ;;
    --env-file=*)
      CONFIG_ENV_FILE="${1#*=}"
      EXPLICIT_CONFIG_FILE=1
      if [[ -z "$CONFIG_ENV_FILE" ]]; then
        echo "--env-file requires a configuration file path." >&2
        exit 1
      fi
      shift
      ;;
    --branch)
      UPDATE_BRANCH="${2:-}"
      shift 2
      ;;
    --branch=*)
      UPDATE_BRANCH="${1#*=}"
      shift
      ;;
    --ref|--tag)
      INSTALL_REF="${2:-}"
      shift 2
      ;;
    --ref=*|--tag=*)
      INSTALL_REF="${1#*=}"
      shift
      ;;
    --install-systemd)
      AUTO_INSTALL_SYSTEMD=1
      shift
      ;;
    --from-source)
      FROM_SOURCE=1
      shift
      ;;
    --force)
      FORCE_REINSTALL=1
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage:
  scripts/install.sh [--mode simple] [--branch main|dev] [--ref <git-ref>] [--non-interactive] [--install-systemd] [--force] [--env-file <path>] [--progress-json]

Modes:
  simple    Host install via venv + systemd. Supports same-host runtime deployment.
  portable  Temporarily unsupported while the separate backend is integrated.

Flags:
  --branch            Default update branch for this installation
  --ref, --tag        Published release tag; Git refs are available with --from-source
  --non-interactive   Fail instead of prompting for missing values
  --install-systemd   In simple mode, start backend, worker, and aiogram client automatically
  --from-source       Development only: export a Git checkout instead of downloading a release archive
  --force             Reinstall even if the target release is already active
  --env-file          Read and update this private installer config instead of the checkout .env
  --progress-json     Emit NODE_PLANE_EVENT JSON progress for workstation clients
                      Existing active installs are refused; use the update workflow
EOF
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [[ "$CONFIG_ENV_FILE" != /* ]]; then
  CONFIG_ENV_FILE="${PWD}/${CONFIG_ENV_FILE}"
fi
if [[ -n "${NODE_PLANE_INSTALL_ENV_FILE:-}" ]]; then
  EXPLICIT_CONFIG_FILE=1
fi
install_progress_init
install_progress_begin configuration
if [[ "${NODE_PLANE_INSTALL_EVENTS:-0}" == "1" ]]; then
  if [[ $NON_INTERACTIVE -ne 1 ]]; then
    echo "Machine-readable progress requires --non-interactive; collect configuration before running the installer." >&2
    exit 1
  fi
  if [[ "$AUTO_SETUP_DRIVER_AGENTS_ON_INSTALL" != "1" ]]; then
    echo "Workstation installation requires driver setup. Unset NODE_PLANE_AUTO_SETUP_DRIVER_AGENTS_ON_INSTALL or set it to 1." >&2
    exit 1
  fi
fi
cd "$REPO_ROOT"

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

read_version() {
  if [[ -f "${REPO_ROOT}/VERSION" ]]; then
    tr -d '\n' < "${REPO_ROOT}/VERSION"
  else
    echo "0.1.0"
  fi
}

prompt_value() {
  local prompt="$1"
  local current_value="$2"
  local result=""
  if [[ $NON_INTERACTIVE -eq 1 ]]; then
    echo "$current_value"
    return 0
  fi
  if [[ -n "$current_value" ]]; then
    read -r -p "$prompt [$current_value]: " result
    if [[ -z "$result" ]]; then
      result="$current_value"
    fi
  else
    read -r -p "$prompt: " result
  fi
  echo "$result"
}

ensure_env_file() {
  if [[ "${NODE_PLANE_INSTALL_EVENTS:-0}" == "1" && -L "$CONFIG_ENV_FILE" ]]; then
    echo "Installer configuration must be a regular file, not a symlink." >&2
    exit 1
  fi
  if [[ ! -f "$CONFIG_ENV_FILE" ]]; then
    if [[ $EXPLICIT_CONFIG_FILE -eq 1 ]]; then
      echo "Installer configuration file is missing. Supply a readable file with --env-file." >&2
      exit 1
    fi
    cp .env.example "$CONFIG_ENV_FILE"
    echo "Created .env from .env.example"
  fi
  if [[ ! -r "$CONFIG_ENV_FILE" || ! -w "$CONFIG_ENV_FILE" ]]; then
    echo "Installer configuration file must be readable and writable." >&2
    exit 1
  fi
  if [[ "${NODE_PLANE_INSTALL_EVENTS:-0}" == "1" ]]; then
    chmod 600 "$CONFIG_ENV_FILE"
  fi
}

read_env_value() {
  local key="$1"
  local file="${2:-$CONFIG_ENV_FILE}"
  if [[ ! -f "$file" ]]; then
    return 0
  fi
  sed -n "s/^${key}=//p" "$file" | tail -n 1
}

set_env_value() {
  local key="$1"
  local value="$2"
  local file="${3:-$CONFIG_ENV_FILE}"
  if [[ ! -f "$file" ]]; then
    touch "$file"
  fi
  if grep -q "^${key}=" "$file"; then
    sed -i "s|^${key}=.*$|${key}=${value}|" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

set_env_value_in_file() {
  local file="$1"
  local key="$2"
  local value="$3"
  set_env_value "$key" "$value" "$file"
}

normalize_update_branch() {
  local branch="${1:-}"
  branch="$(printf '%s' "$branch" | tr '[:upper:]' '[:lower:]')"
  case "$branch" in
    main|dev)
      echo "$branch"
      ;;
    "")
      echo "main"
      ;;
    *)
      echo "Unsupported update branch: $branch" >&2
      exit 1
      ;;
  esac
}

ensure_common_dirs() {
  mkdir -p data ssh scripts
}

fetch_origin_refs() {
  need_cmd git
  if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "The installer source checkout is not a git repository." >&2
    exit 1
  fi
  if [[ "${NODE_PLANE_INSTALL_EVENTS:-0}" == "1" ]]; then
    need_cmd timeout
    GIT_TERMINAL_PROMPT=0 timeout 300 git fetch --quiet --prune --tags origin
  else
    git fetch --quiet --prune --tags origin
  fi
}

print_repo_location_note() {
  echo
  echo "Current repository path:"
  echo "  ${REPO_ROOT}"
}

path_is_within() {
  local child="$1"
  local parent="$2"
  [[ "$child" == "$parent" || "$child" == "$parent/"* ]]
}

current_git_commit() {
  local ref="${1:-HEAD}"
  if [[ -n "${CONTROLLER_COMMIT:-}" ]]; then echo "$CONTROLLER_COMMIT"; return; fi
  if command -v git >/dev/null 2>&1 && git rev-parse --short "$ref" >/dev/null 2>&1; then
    git rev-parse --short "$ref"
  else
    echo "unknown"
  fi
}

current_semver() {
  local ref="${1:-HEAD}"
  local value
  if [[ -n "${CONTROLLER_VERSION:-}" ]]; then echo "$CONTROLLER_VERSION"; return; fi
  if [[ "$ref" == "HEAD" && -f "${REPO_ROOT}/VERSION" ]]; then
    tr -d '\n' < "${REPO_ROOT}/VERSION"
    return 0
  fi
  value="$(git show "${ref}:VERSION" 2>/dev/null | tr -d '\n' || true)"
  if [[ -n "$value" ]]; then
    echo "$value"
  else
    echo "0.1.0"
  fi
}

latest_release_tag_for_branch() {
  local branch="$1"
  if [[ -n "${CONTROLLER_REF:-}" ]]; then echo "$CONTROLLER_REF"; return; fi
  local regex
  case "$branch" in
    main) regex='^v?[0-9]+\.[0-9]+\.[0-9]+$' ;;
    dev) regex='^v?[0-9]+\.[0-9]+\.[0-9]+(-alpha\.[0-9]+)?$' ;;
    *) echo "Unsupported update branch: $branch" >&2; exit 1 ;;
  esac
  while IFS= read -r tag; do
    [[ -z "$tag" ]] && continue
    if [[ "$tag" =~ $regex ]]; then
      echo "$tag"
      return 0
    fi
  done < <(git tag --merged "origin/${branch}" --sort=-version:refname)
  echo "No release tag found for branch '${branch}'." >&2
  exit 1
}

validate_install_ref() {
  local branch="$1"
  local ref="$2"
  if [[ -n "${CONTROLLER_REF:-}" ]]; then
    [[ "${ref#v}" == "${CONTROLLER_REF#v}" ]] || { echo "Selected archive does not match install ref" >&2; return 1; }
    echo "$CONTROLLER_REF"
    return
  fi

  if [[ -z "$ref" ]]; then
    echo "Install ref cannot be empty." >&2
    exit 1
  fi
  if ! git rev-parse --verify "${ref}^{commit}" >/dev/null 2>&1; then
    echo "Unknown install ref: ${ref}" >&2
    exit 1
  fi
  if ! git merge-base --is-ancestor "${ref}^{commit}" "origin/${branch}" >/dev/null 2>&1; then
    echo "Install ref '${ref}' is not reachable from origin/${branch}." >&2
    exit 1
  fi
  echo "$ref"
}


resolve_install_ref() {
  local branch="$1"
  local requested_ref="${2:-}"
  if [[ -z "$requested_ref" ]]; then
    latest_release_tag_for_branch "$branch"
    return 0
  fi
  validate_install_ref "$branch" "$requested_ref"
}

release_id() {
  local ref="${1:-HEAD}"
  local semver commit
  semver="$(current_semver "$ref")"
  commit="$(current_git_commit "$ref")"
  if [[ "$commit" == "unknown" ]]; then
    echo "${semver}-$(date +%Y%m%d%H%M%S)"
  else
    echo "${semver}-${commit}"
  fi
}

read_release_version() {
  local release_dir="$1"
  if [[ -f "${release_dir}/VERSION" ]]; then
    tr -d '\n' < "${release_dir}/VERSION"
  else
    echo ""
  fi
}

read_release_commit() {
  local release_dir="$1"
  if [[ -f "${release_dir}/BUILD_COMMIT" ]]; then
    tr -d '\n' < "${release_dir}/BUILD_COMMIT"
  else
    echo ""
  fi
}

release_matches_target() {
  local release_dir="$1"
  local target_version="$2"
  local target_commit="$3"
  [[ -d "$release_dir" ]] || return 1
  [[ "$(read_release_version "$release_dir")" == "$target_version" ]] || return 1
  [[ "$(read_release_commit "$release_dir")" == "$target_commit" ]] || return 1
}

workstation_target_is_partial() {
  local base_dir="$1" ref="$2" expected
  local marker="${base_dir}/shared/data/workstation-install.incomplete"
  [[ -f "$marker" && ! -L "$marker" ]] || return 1
  expected="node-plane-workstation-install-v1 $(current_semver "$ref") $(current_git_commit "$ref")"
  [[ "$(cat "$marker")" == "$expected" ]] || return 1
  release_matches_target "${base_dir}/current" "$(current_semver "$ref")" "$(current_git_commit "$ref")"
}

export_release_tree() {
  local destination="$1"
  local ref="${2:-HEAD}"
  if [[ -n "${CONTROLLER_STAGE_DIR:-}" ]]; then
    cp -a "$CONTROLLER_STAGE_DIR" "$destination"
    return
  fi
  mkdir -p "$destination"
  if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git archive "$ref" | tar -xf - -C "$destination"
  else
    tar \
      --exclude='.git' \
      --exclude='.venv' \
      --exclude='data' \
      --exclude='ssh' \
      --exclude='releases' \
      --exclude='current' \
      --exclude='shared' \
      -cf - . | tar -xf - -C "$destination"
  fi
  printf '%s\n' "$(current_git_commit "$ref")" > "${destination}/BUILD_COMMIT"
}

sync_shared_env() {
  local shared_dir="$1"
  local existing_token_file=""
  mkdir -p "$shared_dir"
  if [[ "${NODE_PLANE_INSTALL_EVENTS:-0}" == "1" \
    && ( -L "${shared_dir}/.env" || ( -e "${shared_dir}/.env" && ! -f "${shared_dir}/.env" ) ) ]]; then
    echo "Shared runtime configuration must be a regular file, not a symlink." >&2
    exit 1
  fi
  if [[ -f "${shared_dir}/.env" ]]; then
    existing_token_file="$(read_env_value NODE_PLANE_BACKEND_ADAPTER_TOKEN_FILE "${shared_dir}/.env")"
  fi
  if [[ "${NODE_PLANE_INSTALL_EVENTS:-0}" == "1" && -f "${shared_dir}/.env" ]]; then
    # A retry can encounter a partially provisioned database/adapter credential.
    # Preserve that state; fill only missing values from the uploaded config.
    local line key value
    while IFS= read -r line || [[ -n "$line" ]]; do
      [[ "$line" =~ ^([A-Z][A-Z0-9_]*)=(.*)$ ]] || continue
      key="${BASH_REMATCH[1]}"
      value="${BASH_REMATCH[2]}"
      if [[ -z "$(read_env_value "$key" "${shared_dir}/.env")" ]]; then
        set_env_value_in_file "${shared_dir}/.env" "$key" "$value"
      fi
    done < "$CONFIG_ENV_FILE"
    set_env_value_in_file "${shared_dir}/.env" NODE_PLANE_INSTALL_REF "$(read_env_value NODE_PLANE_INSTALL_REF)"
  else
    cp "$CONFIG_ENV_FILE" "${shared_dir}/.env"
  fi
  if [[ -z "$(read_env_value NODE_PLANE_BACKEND_ADAPTER_TOKEN_FILE)" \
    && -n "$existing_token_file" && -f "$existing_token_file" && -r "$existing_token_file" ]]; then
    set_env_value_in_file "${shared_dir}/.env" NODE_PLANE_BACKEND_ADAPTER_TOKEN_FILE "$existing_token_file"
  fi
  if [[ "${NODE_PLANE_INSTALL_EVENTS:-0}" == "1" ]]; then
    chmod 600 "${shared_dir}/.env"
  fi
}

prepare_telegram_identity() {
  local release_dir="$1" base_dir="$2" shared_dir="$3" env_file="${shared_dir}/.env"
  local admin_ids admin_id token_file postgres_dsn
  admin_ids="$(read_env_value ADMIN_IDS "$env_file")"
  postgres_dsn="$(read_env_value POSTGRES_DSN "$env_file")"
  IFS=',' read -ra ids <<< "$admin_ids"
  for admin_id in "${ids[@]}"; do
    admin_id="${admin_id//[[:space:]]/}"
    if [[ ! "$admin_id" =~ ^[0-9]+$ ]]; then
      echo "Invalid Telegram administrator ID: ${admin_id}" >&2
      return 1
    fi
    NODE_PLANE_BASE_DIR="$base_dir" NODE_PLANE_APP_DIR="$release_dir" \
      NODE_PLANE_SHARED_DIR="$shared_dir" DB_BACKEND=postgres POSTGRES_DSN="$postgres_dsn" \
      PYTHONPATH="${release_dir}/app" \
      "${release_dir}/.venv/bin/python" -m backend.admin_cli bootstrap-admin --telegram-id "$admin_id"
  done

  token_file="$(read_env_value NODE_PLANE_BACKEND_ADAPTER_TOKEN_FILE "$env_file")"
  if [[ -z "$token_file" ]]; then
    token_file="${shared_dir}/data/telegram-adapter.token"
  fi
  if [[ -e "$token_file" && ! -f "$token_file" ]]; then
    echo "Adapter credential path is not a regular file: $token_file" >&2
    return 1
  fi
  if [[ -e "$token_file" && ! -r "$token_file" ]]; then
    echo "Adapter credential is not readable: $token_file" >&2
    return 1
  fi
  if [[ ! -f "$token_file" ]]; then
    mkdir -p "$(dirname "$token_file")"
    NODE_PLANE_BASE_DIR="$base_dir" NODE_PLANE_APP_DIR="$release_dir" \
      NODE_PLANE_SHARED_DIR="$shared_dir" DB_BACKEND=postgres POSTGRES_DSN="$postgres_dsn" \
      PYTHONPATH="${release_dir}/app" \
      "${release_dir}/.venv/bin/python" -m backend.admin_cli issue-token --kind adapter --output "$token_file"
  fi
  set_env_value_in_file "$env_file" NODE_PLANE_BACKEND_ADAPTER_TOKEN_FILE "$token_file"
  set_env_value NODE_PLANE_BACKEND_ADAPTER_TOKEN_FILE "$token_file"
}

choose_mode() {
  if [[ -n "$MODE" ]]; then
    return 0
  fi
  if [[ $NON_INTERACTIVE -eq 1 ]]; then
    echo "Mode is required in non-interactive mode. Use --mode simple." >&2
    exit 1
  fi
  echo "Choose installation mode:"
  echo "  1) simple   Host install via systemd, supports same-host runtime deployment"
  read -r -p "Enter 1 [1]: " selection
  case "${selection:-1}" in
    1) MODE="simple" ;;
    *) echo "Unsupported selection: ${selection}" >&2; exit 1 ;;
  esac
}

configure_env() {
  ensure_env_file
  ensure_common_dirs
  set_step "read installer environment"

  local bot_token admin_ids base_dir app_dir shared_dir source_dir install_mode ssh_key image_repo image_tag update_branch install_ref latest_install_ref
  local db_backend postgres_dsn
  bot_token="$(read_env_value BOT_TOKEN)"
  admin_ids="$(read_env_value ADMIN_IDS)"
  base_dir="$(read_env_value NODE_PLANE_BASE_DIR)"
  app_dir="$(read_env_value NODE_PLANE_APP_DIR)"
  shared_dir="$(read_env_value NODE_PLANE_SHARED_DIR)"
  source_dir="$(read_env_value NODE_PLANE_SOURCE_DIR)"
  install_mode="$(read_env_value NODE_PLANE_INSTALL_MODE)"
  ssh_key="$(read_env_value SSH_KEY)"
  image_repo="$(read_env_value NODE_PLANE_IMAGE_REPO)"
  image_tag="$(read_env_value NODE_PLANE_IMAGE_TAG)"
  # .env records the previous installation's ref; only an explicit CLI/env
  # override should pin a future installation to that same version.
  install_ref="$INSTALL_REF"
  db_backend="$(read_env_value DB_BACKEND)"
  postgres_dsn="$(read_env_value POSTGRES_DSN)"
  update_branch="${UPDATE_BRANCH:-$(read_env_value NODE_PLANE_UPDATE_BRANCH)}"
  update_branch="$(normalize_update_branch "$update_branch")"

  if [[ -z "$db_backend" ]]; then
    db_backend="postgres"
  fi
  if [[ "$db_backend" != "postgres" ]]; then
    echo "Unsupported DB_BACKEND for 0.4: ${db_backend}" >&2
    exit 1
  fi

  if [[ -z "$bot_token" || "$bot_token" == "replace_me" ]]; then
    bot_token="$(prompt_value "Enter BOT_TOKEN" "")"
  elif [[ $NON_INTERACTIVE -eq 0 ]]; then
    bot_token="$(prompt_value "Enter BOT_TOKEN" "$bot_token")"
  fi

  if [[ -z "$bot_token" || "$bot_token" == "replace_me" ]]; then
    echo "BOT_TOKEN is required." >&2
    exit 1
  fi

  if [[ -z "$admin_ids" || "$admin_ids" == "123456789" ]]; then
    admin_ids="$(prompt_value "Enter ADMIN_IDS (comma-separated Telegram numeric user ids)" "")"
  elif [[ $NON_INTERACTIVE -eq 0 ]]; then
    admin_ids="$(prompt_value "Enter ADMIN_IDS (comma-separated Telegram numeric user ids)" "$admin_ids")"
  fi

  if [[ -z "$admin_ids" ]]; then
    echo "ADMIN_IDS is required." >&2
    exit 1
  fi
  if [[ ! "$admin_ids" =~ ^[[:space:]]*[0-9]+[[:space:]]*(,[[:space:]]*[0-9]+[[:space:]]*)*$ ]]; then
    echo "ADMIN_IDS must contain comma-separated numeric Telegram user IDs." >&2
    exit 1
  fi

  if [[ $NON_INTERACTIVE -eq 0 ]]; then
    update_branch="$(prompt_value "Enter default update branch (main or dev)" "$update_branch")"
    update_branch="$(normalize_update_branch "$update_branch")"
  fi
  echo "Selecting controller release..."
  set_step "prepare controller host prerequisites"
  ensure_controller_host_tools
  set_step "fetch release tags"
  if [[ "$FROM_SOURCE" == 1 ]]; then
    fetch_origin_refs
  else
    set_step "download and verify controller archive"
    prepare_controller_archive "$update_branch" "$install_ref" 1
  fi
  set_step "select install ref"
  latest_install_ref="$(latest_release_tag_for_branch "$update_branch")"
  if [[ -z "$install_ref" ]]; then
    install_ref="$latest_install_ref"
  fi
  if [[ $NON_INTERACTIVE -eq 0 ]]; then
    install_ref="$(prompt_value "Enter install tag/ref (default: latest tag for ${update_branch})" "$install_ref")"
  fi
  if [[ "$FROM_SOURCE" == 0 && "${install_ref#v}" != "${CONTROLLER_REF#v}" ]]; then
    cleanup_controller_archive
    prepare_controller_archive "$update_branch" "$install_ref" 1
  fi
  install_ref="$(resolve_install_ref "$update_branch" "$install_ref")"

  if [[ "$MODE" == "simple" ]]; then
    install_mode="simple"
    if [[ -z "$base_dir" ]]; then
      base_dir="${REPO_ROOT}"
    fi
    if [[ -z "$app_dir" ]]; then
      app_dir="${base_dir}/current"
    fi
    if [[ -z "$shared_dir" ]]; then
      shared_dir="${base_dir}/shared"
    fi

    if [[ "$base_dir" != "$REPO_ROOT" && $NON_INTERACTIVE -eq 0 ]]; then
      echo
      echo "Detected NODE_PLANE_BASE_DIR in .env:"
      echo "  ${base_dir}"
      echo "Current repository path is:"
      echo "  ${REPO_ROOT}"
      echo "This checkout will be exported into releases under the install root."
    fi
    if [[ $NON_INTERACTIVE -eq 0 ]]; then
      base_dir="$(prompt_value "Enter NODE_PLANE_BASE_DIR install root for systemd mode" "$base_dir")"
      app_dir="$(prompt_value "Enter NODE_PLANE_APP_DIR for the active release symlink" "${base_dir}/current")"
      shared_dir="$(prompt_value "Enter NODE_PLANE_SHARED_DIR for runtime data and SSH keys" "${base_dir}/shared")"
    else
      app_dir="${base_dir}/current"
      shared_dir="${base_dir}/shared"
    fi
    if [[ "$app_dir" != "${base_dir}/current" ]]; then
      echo "Simple mode expects NODE_PLANE_APP_DIR to be ${base_dir}/current." >&2
      exit 1
    fi
    if [[ "$shared_dir" != "${base_dir}/shared" ]]; then
      if [[ $NON_INTERACTIVE -eq 1 ]]; then
        echo "Simple mode expects NODE_PLANE_SHARED_DIR to be ${base_dir}/shared." >&2
        exit 1
      fi
      echo "Simple mode expects NODE_PLANE_SHARED_DIR to be ${base_dir}/shared."
      shared_dir="${base_dir}/shared"
    fi
    if path_is_within "$REPO_ROOT" "$base_dir"; then
      echo >&2
      echo "The current checkout is inside NODE_PLANE_BASE_DIR." >&2
      echo "That mixes source files with release artifacts and is not recommended." >&2
      echo "Use separate paths, for example:" >&2
      echo "  source checkout: /opt/node-plane-src" >&2
      echo "  install root:    ${base_dir}" >&2
      echo "Then rerun ./scripts/install.sh from the source checkout." >&2
      exit 1
    fi
    if [[ "$base_dir" != /* ]]; then
      echo "NODE_PLANE_BASE_DIR must be an absolute path." >&2
      exit 1
    fi
    if [[ "${NODE_PLANE_INSTALL_EVENTS:-0}" == "1" \
      && ( -e "${base_dir}/current" || -L "${base_dir}/current" ) ]] \
      && ! workstation_target_is_partial "$base_dir" "$install_ref"; then
      echo "An active installation already exists. Use the update workflow; workstation installs never replace it." >&2
      exit 1
    fi
    if [[ "${NODE_PLANE_INSTALL_EVENTS:-0}" == "1" && -f "${shared_dir}/.env" ]]; then
      local previous_token previous_admin_ids
      previous_token="$(read_env_value BOT_TOKEN "${shared_dir}/.env")"
      previous_admin_ids="$(read_env_value ADMIN_IDS "${shared_dir}/.env")"
      if [[ ( -n "$previous_token" && "$previous_token" != "$bot_token" ) \
        || ( -n "$previous_admin_ids" && "$previous_admin_ids" != "$admin_ids" ) ]]; then
        echo "A previous partial installation has different bot or administrator credentials. Reuse its configuration; shared state was not changed." >&2
        exit 1
      fi
    fi
    set_env_value NODE_PLANE_BASE_DIR "$base_dir"
    set_env_value NODE_PLANE_APP_DIR "$app_dir"
    set_env_value NODE_PLANE_SHARED_DIR "$shared_dir"
    if [[ "$FROM_SOURCE" == 0 ]]; then
      set_env_value NODE_PLANE_SOURCE_DIR "$app_dir"
    else
      set_env_value NODE_PLANE_SOURCE_DIR "$REPO_ROOT"
    fi
    set_env_value NODE_PLANE_INSTALL_MODE "$install_mode"
    set_env_value NODE_PLANE_UPDATE_BRANCH "$update_branch"
    set_env_value NODE_PLANE_INSTALL_REF "$install_ref"
  else
    install_mode="portable"
    if [[ -n "$base_dir" && $NON_INTERACTIVE -eq 0 ]]; then
      base_dir="$(prompt_value "Enter NODE_PLANE_BASE_DIR used inside the container" "$base_dir")"
      set_env_value NODE_PLANE_BASE_DIR "$base_dir"
    fi
    if [[ -z "$ssh_key" ]]; then
      ssh_key="/root/.ssh/id_ed25519"
    fi
    if [[ $NON_INTERACTIVE -eq 0 ]]; then
      ssh_key="$(prompt_value "Enter SSH_KEY for remote node management" "$ssh_key")"
    fi
    if [[ -z "$image_repo" ]]; then
      image_repo="ghcr.io/saharoktyan/node-plane"
    fi
    if [[ -z "$image_tag" ]]; then
      image_tag="$(current_semver "$install_ref")"
    fi
    if [[ $NON_INTERACTIVE -eq 0 ]]; then
      image_repo="$(prompt_value "Enter NODE_PLANE_IMAGE_REPO (default: ghcr.io/saharoktyan/node-plane, or use node-plane for local builds)" "$image_repo")"
      image_tag="$(prompt_value "Enter NODE_PLANE_IMAGE_TAG (use local for local builds)" "$image_tag")"
    fi
    set_env_value SSH_KEY "$ssh_key"
    set_env_value NODE_PLANE_SOURCE_DIR "$REPO_ROOT"
    set_env_value NODE_PLANE_INSTALL_MODE "$install_mode"
    set_env_value NODE_PLANE_UPDATE_BRANCH "$update_branch"
    set_env_value NODE_PLANE_INSTALL_REF "$install_ref"
    set_env_value NODE_PLANE_IMAGE_REPO "$image_repo"
    set_env_value NODE_PLANE_IMAGE_TAG "$image_tag"
  fi

  set_env_value BOT_TOKEN "$bot_token"
  set_env_value ADMIN_IDS "$admin_ids"
  set_env_value DB_BACKEND "$db_backend"
  if [[ "$MODE" == "portable" ]]; then
    ensure_portable_postgres_env ".env"
  elif [[ -n "$postgres_dsn" ]]; then
    set_env_value POSTGRES_DSN "$postgres_dsn"
  fi
}

validate_simple_layout() {
  local app_dir="$1"
  local shared_dir="$2"
  local missing=0

  echo
  echo "Validating simple mode layout..."

  if [[ ! -d "$app_dir" ]]; then
    echo "Missing app directory: $app_dir" >&2
    missing=1
  fi
  if [[ ! -f "$shared_dir/.env" ]]; then
    echo "Missing environment file: $shared_dir/.env" >&2
    missing=1
  fi
  if [[ ! -f "$app_dir/app/telegram_client/main.py" ]]; then
    echo "Missing Telegram client entrypoint: $app_dir/app/telegram_client/main.py" >&2
    missing=1
  fi
  if [[ ! -x "$app_dir/.venv/bin/python" ]]; then
    echo "Missing virtualenv python: $app_dir/.venv/bin/python" >&2
    missing=1
  fi

  if [[ $missing -ne 0 ]]; then
    echo
    echo "Simple mode validation failed."
    echo "The generated systemd unit would point to files that do not exist."
    echo "Either:"
    echo "  1. rerun the installer so it can recreate the active release under ${app_dir}"
    echo "  2. or verify NODE_PLANE_SHARED_DIR and the exported release layout"
    exit 1
  fi

  echo "Simple mode layout looks valid."
}

ensure_release_python_runtime() {
  set_step "install python dependencies with uv"
  install_release_dependencies "$1" "$2"
}

run_simple_install() {
  local base_dir app_dir shared_dir releases_dir current_link new_release_dir release_name install_ref install_version install_commit reused_release previous_release driver_ready=1
  local runtime_env_file db_backend postgres_dsn
  base_dir="$(read_env_value NODE_PLANE_BASE_DIR)"
  app_dir="$(read_env_value NODE_PLANE_APP_DIR)"
  shared_dir="$(read_env_value NODE_PLANE_SHARED_DIR)"
  if [[ -z "$base_dir" ]]; then
    base_dir="$REPO_ROOT"
  fi
  if [[ -z "$app_dir" ]]; then
    app_dir="${base_dir}/current"
  fi
  if [[ -z "$shared_dir" ]]; then
    shared_dir="${base_dir}/shared"
  fi
  if [[ -z "$base_dir" ]]; then
    echo "NODE_PLANE_BASE_DIR is required for simple mode." >&2
    exit 1
  fi
  releases_dir="${base_dir}/releases"
  current_link="${base_dir}/current"
  previous_release=""
  if [[ -d "$current_link" ]]; then
    previous_release="$(readlink -f "$current_link")"
  fi
  install_ref="$(
    resolve_install_ref \
      "$(normalize_update_branch "$(read_env_value NODE_PLANE_UPDATE_BRANCH)")" \
      "$(read_env_value NODE_PLANE_INSTALL_REF)"
  )"
  install_version="$(current_semver "$install_ref")"
  install_commit="$(current_git_commit "$install_ref")"
  release_name="$(release_id "$install_ref")"
  new_release_dir="${releases_dir}/${release_name}"
  reused_release=0

  install_progress_begin release
  mkdir -p "${releases_dir}" "${shared_dir}/data" "${shared_dir}/ssh"
  if [[ "${NODE_PLANE_INSTALL_EVENTS:-0}" == "1" ]]; then
    need_cmd flock
    local install_lock_fd
    exec {install_lock_fd}>"${shared_dir}/data/workstation-install.lock"
    if ! flock -n "$install_lock_fd"; then
      echo "Another workstation installation is running. Wait for it to finish before retrying." >&2
      exit 1
    fi
    # Recheck after taking the lock, before synchronizing configuration.
    if [[ ( -e "$current_link" || -L "$current_link" ) ]] \
      && ! workstation_target_is_partial "$base_dir" "$install_ref"; then
      echo "An active installation already exists. Use the update workflow; workstation installs never replace it." >&2
      exit 1
    fi
    WORKSTATION_INSTALL_MARKER="${shared_dir}/data/workstation-install.incomplete"
    if [[ -L "$WORKSTATION_INSTALL_MARKER" || ( -e "$WORKSTATION_INSTALL_MARKER" && ! -f "$WORKSTATION_INSTALL_MARKER" ) ]]; then
      echo "The incomplete-install marker is not a regular file. Shared state was not changed." >&2
      exit 1
    fi
    local marker_temporary
    marker_temporary="$(mktemp "${WORKSTATION_INSTALL_MARKER}.XXXXXX")"
    chmod 600 "$marker_temporary"
    printf 'node-plane-workstation-install-v1 %s %s\n' "$install_version" "$install_commit" > "$marker_temporary"
    mv -f "$marker_temporary" "$WORKSTATION_INSTALL_MARKER"
  fi
  sync_shared_env "$shared_dir"
  runtime_env_file="${shared_dir}/.env"
  if [[ $FORCE_REINSTALL -eq 0 ]] && release_matches_target "$current_link" "$install_version" "$install_commit"; then
    # The physical directory avoids replacing current with a symlink to itself
    # when retrying a partially activated installation.
    new_release_dir="$(cd "$current_link" && pwd -P)"
    reused_release=1
  elif [[ $FORCE_REINSTALL -eq 0 ]] && release_matches_target "$new_release_dir" "$install_version" "$install_commit"; then
    reused_release=1
  else
    if [[ "${NODE_PLANE_INSTALL_EVENTS:-0}" == "1" \
      && ( -e "$new_release_dir" || -L "$new_release_dir" ) ]]; then
      echo "The target release directory contains unrecognized files. Workstation installs will not delete them." >&2
      exit 1
    fi
    rm -rf "$new_release_dir"
    set_step "export release tree"
    export_release_tree "$new_release_dir" "$install_ref"
  fi

  install_progress_done
  install_progress_begin python
  set_step "select python runtime"
  PYTHON_BIN="$(select_controller_python "$shared_dir")"
  echo "Using Python runtime: ${PYTHON_BIN}"
  ensure_release_python_runtime "$new_release_dir" "$shared_dir"
  install_progress_done

  # DB runtime init must run even when we reuse an existing release tree.
  # Otherwise a previous partial install can leave DB_BACKEND=postgres without
  # POSTGRES_DSN and the service will fail at import-time.
  install_progress_begin database
  set_step "load database runtime configuration"
  db_backend="$(read_env_value DB_BACKEND "$runtime_env_file")"
  db_backend="${db_backend:-postgres}"
  if [[ "$db_backend" != postgres ]]; then
    echo "Only PostgreSQL runtime is supported." >&2
    exit 1
  fi
  set_step "auto-provision local postgresql runtime"
  auto_provision_simple_postgres "$runtime_env_file" "$shared_dir" 1
  postgres_dsn="$(read_env_value POSTGRES_DSN "$runtime_env_file")"
  if [[ -z "$postgres_dsn" ]]; then
    echo "POSTGRES_DSN is required." >&2
    exit 1
  fi
  if [[ -f "${new_release_dir}/app/backend/admin_cli.py" ]]; then
    set_step "initialize backend schema"
    NODE_PLANE_BASE_DIR="${base_dir}" \
    NODE_PLANE_APP_DIR="${new_release_dir}" \
    NODE_PLANE_SHARED_DIR="${shared_dir}" \
    DB_BACKEND="postgres" \
    POSTGRES_DSN="$(read_env_value POSTGRES_DSN "$runtime_env_file")" \
    PYTHONPATH="${new_release_dir}/app" \
    "${new_release_dir}/.venv/bin/python" -m backend.admin_cli init-schema
    install_progress_done
    install_progress_begin identity
    set_step "prepare Telegram administrators and adapter credential"
    prepare_telegram_identity "$new_release_dir" "$base_dir" "$shared_dir"
  else
    echo "This release does not contain the separate backend installer. Select a supported release." >&2
    exit 1
  fi
  install_progress_done

  install_progress_begin services
  ln -sfn "$new_release_dir" "$current_link"

  validate_simple_layout "$current_link" "$shared_dir"

  echo
  echo "Simple mode environment is prepared."
  echo
  if [[ $reused_release -eq 1 ]]; then
    echo "Installer status:"
    echo "  Target release is already installed; reusing existing files."
    echo
  fi
  echo "Installed ref:"
  echo "  ${install_ref}"
  echo "Installed version:"
  echo "  ${install_version}"
  echo
  echo "Install root:"
  echo "  ${base_dir}"
  echo "Active release:"
  echo "  ${new_release_dir}"
  echo "Shared state:"
  echo "  ${shared_dir}"
  echo
  echo "Systemd services: node-plane-backend, node-plane-backend-worker.timer, node-plane-telegram"
  echo
  if [[ $AUTO_INSTALL_SYSTEMD -eq 1 || $NON_INTERACTIVE -eq 1 ]]; then
    install_systemd_stack "$current_link" "$base_dir" "$shared_dir"
  elif [[ $NON_INTERACTIVE -eq 0 ]]; then
    local answer
    read -r -p "Install and start the backend, worker, and Telegram client now? [Y/n]: " answer
    if [[ "${answer:-y}" =~ ^([Yy]|[Yy][Ee][Ss])$ ]]; then
      install_systemd_stack "$current_link" "$base_dir" "$shared_dir"
    fi
  fi
  install_progress_done

  install_progress_begin driver
  if [[ "$AUTO_SETUP_DRIVER_AGENTS_ON_INSTALL" == "1" ]]; then
    echo
    echo "Running post-install driver/agent setup (best-effort)..."
    if ! NODE_PLANE_BASE_DIR="${base_dir}" \
      NODE_PLANE_APP_DIR="${current_link}" \
      NODE_PLANE_SHARED_DIR="${shared_dir}" \
      "${current_link}/scripts/setup_driver_agents.sh"; then
      if [[ "${NODE_PLANE_INSTALL_EVENTS:-0}" == "1" ]]; then
        echo "Driver/agent setup failed. The services may be installed, but setup is incomplete; inspect the setup log." >&2
        exit 1
      fi
      echo "Driver/agent setup reported issues. Continuing because best-effort is enabled." >&2
      driver_ready=0
    fi
  fi
  if [[ -n "$WORKSTATION_INSTALL_MARKER" ]]; then
    rm -f "$WORKSTATION_INSTALL_MARKER"
  fi
  install_progress_done

  if [[ $AUTO_INSTALL_SYSTEMD -eq 1 && $driver_ready -eq 1 ]]; then
    retain_successful_releases "$base_dir" "$new_release_dir" "$previous_release" "$shared_dir"
  fi

  echo
  echo "First-run path:"
  echo "  1. Controller release is installed at:"
  echo "     ${new_release_dir}"
  echo "     Shared runtime state lives under ${shared_dir}"
  if [[ $AUTO_INSTALL_SYSTEMD -eq 1 ]]; then
    echo "  2. Service install is done. Verify the host setup:"
  else
    echo "  2. Install the backend, worker, and Telegram client, then verify the host setup:"
    echo "     sudo ${current_link}/scripts/install_backend_systemd.sh ${base_dir} ${shared_dir}"
    echo "     sudo ${current_link}/scripts/install_telegram_client_systemd.sh ${base_dir} ${shared_dir} --activate"
  fi
  echo "     ./scripts/healthcheck.sh --mode simple"
  echo "  3. Open the bot from the Telegram account listed in ADMIN_IDS"
  echo "  4. Send /start once"
  echo "     The administrator account is already prepared in the backend"
  echo "  5. Add a node, install its agent, then apply its settings"
}

install_systemd_stack() {
  local current_link="$1" base_dir="$2" shared_dir="$3"
  set_step "install backend services"
  run_as_root bash "${current_link}/scripts/install_backend_systemd.sh" "$base_dir" "$shared_dir"
  set_step "activate Telegram client"
  run_as_root bash "${current_link}/scripts/install_telegram_client_systemd.sh" "$base_dir" "$shared_dir" --activate
  AUTO_INSTALL_SYSTEMD=1
}


choose_mode
case "$MODE" in
  simple) ;;
  portable)
    echo "Portable Docker installation is temporarily unsupported; use --mode simple." >&2
    exit 1
    ;;
  *)
    echo "Unsupported mode: ${MODE}. Use simple or portable." >&2
    exit 1
    ;;
esac

configure_env
install_progress_done
print_repo_location_note

run_simple_install
