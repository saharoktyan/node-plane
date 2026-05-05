#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
source "${SCRIPT_DIR}/postgres_runtime.sh"

MODE="${MODE:-}"
NON_INTERACTIVE=0
AUTO_INSTALL_SYSTEMD=0
UPDATE_BRANCH="${NODE_PLANE_UPDATE_BRANCH:-}"
INSTALL_REF="${NODE_PLANE_INSTALL_REF:-}"
FORCE_REINSTALL=0
CURRENT_STEP="startup"
AUTO_SETUP_DRIVER_AGENTS_ON_INSTALL="${NODE_PLANE_AUTO_SETUP_DRIVER_AGENTS_ON_INSTALL:-0}"

set_step() {
  CURRENT_STEP="$1"
}

on_error() {
  local exit_code="$1"
  echo >&2
  echo "Install failed during step: ${CURRENT_STEP}" >&2
  echo "Failing command: ${BASH_COMMAND}" >&2
  echo "Exit code: ${exit_code}" >&2
}

trap 'on_error $?' ERR

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
    --force)
      FORCE_REINSTALL=1
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage:
  scripts/install.sh [--mode simple|portable] [--branch main|dev] [--ref <git-ref>] [--non-interactive] [--install-systemd] [--force]

Modes:
  simple    Host install via venv + systemd. Supports same-host runtime deployment.
  portable  Docker-based bot install. Intended for remote SSH-managed nodes.

Flags:
  --branch            Default update branch for this installation
  --ref, --tag        Git tag/ref to install, defaults to the latest release tag for the selected branch
  --non-interactive   Fail instead of prompting for missing values
  --install-systemd   In simple mode, install the systemd unit automatically
  --force             Reinstall even if the target release is already active
EOF
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

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

python_version() {
  python3 - <<'EOF'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}")
EOF
}

python_supported() {
  python3 - <<'EOF'
import sys
sys.exit(0 if sys.version_info[:2] in {(3, 11), (3, 12)} else 1)
EOF
}

ensure_supported_python() {
  local version
  version="$(python_version)"
  if python_supported; then
    echo "Detected supported Python runtime: ${version}"
    return 0
  fi

  echo "Unsupported Python runtime detected: ${version}" >&2
  echo "Simple mode currently supports Python 3.11.x and 3.12.x for the bot runtime." >&2
  echo "Install Python 3.11 or 3.12, make it available as python3, and rerun the installer." >&2
  exit 1
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
  if [[ ! -f .env ]]; then
    cp .env.example .env
    echo "Created .env from .env.example"
  fi
}

read_env_value() {
  local key="$1"
  if [[ ! -f .env ]]; then
    return 0
  fi
  sed -n "s/^${key}=//p" .env | tail -n 1
}

set_env_value() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}=" .env; then
    sed -i "s|^${key}=.*$|${key}=${value}|" .env
  else
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
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
  git fetch --quiet --tags origin
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
  if command -v git >/dev/null 2>&1 && git rev-parse --short "$ref" >/dev/null 2>&1; then
    git rev-parse --short "$ref"
  else
    echo "unknown"
  fi
}

current_semver() {
  local ref="${1:-HEAD}"
  local value
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
  local regex
  case "$branch" in
    main) regex='^v?[0-9]+\.[0-9]+\.[0-9]+$' ;;
    dev) regex='^v?[0-9]+\.[0-9]+\.[0-9]+-alpha\.[0-9]+$' ;;
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

ref_supports_manage_db_command() {
  local ref="$1"
  local command_name="$2"
  local manage_db_source

  manage_db_source="$(git show "${ref}:app/manage_db.py" 2>/dev/null || true)"
  [[ -n "$manage_db_source" ]] || return 1
  printf '%s\n' "$manage_db_source" | grep -Fq "\"${command_name}\""
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

export_release_tree() {
  local destination="$1"
  local ref="${2:-HEAD}"
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
  mkdir -p "$shared_dir"
  cp .env "${shared_dir}/.env"
}

choose_mode() {
  if [[ -n "$MODE" ]]; then
    return 0
  fi
  if [[ $NON_INTERACTIVE -eq 1 ]]; then
    echo "Mode is required in non-interactive mode. Use --mode simple or --mode portable." >&2
    exit 1
  fi
  echo "Choose installation mode:"
  echo "  1) simple   Host install via systemd, supports same-host runtime deployment"
  echo "  2) portable Docker install, for remote SSH-managed nodes"
  read -r -p "Enter 1 or 2 [1]: " selection
  case "${selection:-1}" in
    1) MODE="simple" ;;
    2) MODE="portable" ;;
    *) echo "Unsupported selection: ${selection}" >&2; exit 1 ;;
  esac
}

configure_env() {
  ensure_env_file
  ensure_common_dirs
  fetch_origin_refs
  set_step "read installer environment"

  local bot_token admin_ids base_dir app_dir shared_dir source_dir install_mode ssh_key image_repo image_tag update_branch install_ref latest_install_ref
  local db_backend postgres_dsn sqlite_db_path
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
  install_ref="${INSTALL_REF:-$(read_env_value NODE_PLANE_INSTALL_REF)}"
  db_backend="$(read_env_value DB_BACKEND)"
  postgres_dsn="$(read_env_value POSTGRES_DSN)"
  sqlite_db_path="$(read_env_value SQLITE_DB_PATH)"
  update_branch="${UPDATE_BRANCH:-$(read_env_value NODE_PLANE_UPDATE_BRANCH)}"
  update_branch="$(normalize_update_branch "$update_branch")"

  if [[ -z "$db_backend" || "$db_backend" == "sqlite" ]]; then
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

  if [[ $NON_INTERACTIVE -eq 0 ]]; then
    update_branch="$(prompt_value "Enter default update branch (main or dev)" "$update_branch")"
    update_branch="$(normalize_update_branch "$update_branch")"
  fi
  latest_install_ref="$(latest_release_tag_for_branch "$update_branch")"
  if [[ -z "$install_ref" ]]; then
    install_ref="$latest_install_ref"
  fi
  if [[ $NON_INTERACTIVE -eq 0 ]]; then
    install_ref="$(prompt_value "Enter install tag/ref (default: latest tag for ${update_branch})" "$install_ref")"
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
    set_env_value NODE_PLANE_BASE_DIR "$base_dir"
    set_env_value NODE_PLANE_APP_DIR "$app_dir"
    set_env_value NODE_PLANE_SHARED_DIR "$shared_dir"
    set_env_value NODE_PLANE_SOURCE_DIR "$REPO_ROOT"
    set_env_value NODE_PLANE_INSTALL_MODE "$install_mode"
    set_env_value NODE_PLANE_UPDATE_BRANCH "$update_branch"
    set_env_value NODE_PLANE_INSTALL_REF "$install_ref"
    if [[ -z "$sqlite_db_path" ]]; then
      sqlite_db_path="${shared_dir}/data/bot.sqlite3"
    fi
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
      image_repo="ghcr.io/seventh7dev/node-plane"
    fi
    if [[ -z "$image_tag" ]]; then
      image_tag="$(current_semver "$install_ref")"
    fi
    if [[ $NON_INTERACTIVE -eq 0 ]]; then
      image_repo="$(prompt_value "Enter NODE_PLANE_IMAGE_REPO (default: ghcr.io/seventh7dev/node-plane, or use node-plane for local builds)" "$image_repo")"
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
  if [[ -n "$sqlite_db_path" ]]; then
    set_env_value SQLITE_DB_PATH "$sqlite_db_path"
  fi
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
  if [[ ! -f "$app_dir/app/main.py" ]]; then
    echo "Missing app entrypoint: $app_dir/app/main.py" >&2
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
  local release_dir="$1"
  local python_bin="${release_dir}/.venv/bin/python"

  if [[ ! -x "$python_bin" ]]; then
    set_step "create virtualenv"
    python3 -m venv "${release_dir}/.venv"
  fi

  # Keep this idempotent: upgrade tooling and reinstall runtime deps so reused
  # releases cannot keep a partially provisioned virtualenv.
  set_step "install python build tooling"
  "$python_bin" -m pip install --upgrade pip setuptools wheel
  set_step "install python dependencies"
  "$python_bin" -m pip install -r "${release_dir}/requirements.txt"
}

run_simple_install() {
  local service_name="node-plane"
  local base_dir app_dir shared_dir releases_dir current_link new_release_dir release_name install_ref install_version install_commit reused_release
  local runtime_env_file db_backend postgres_dsn sqlite_db_path
  local supports_postgres_migration=0
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
  install_ref="$(
    resolve_install_ref \
      "$(normalize_update_branch "$(read_env_value NODE_PLANE_UPDATE_BRANCH)")" \
      "$(read_env_value NODE_PLANE_INSTALL_REF)"
  )"
  install_version="$(current_semver "$install_ref")"
  install_commit="$(current_git_commit "$install_ref")"
  if ref_supports_manage_db_command "$install_ref" "migrate-to-postgres"; then
    supports_postgres_migration=1
  fi
  release_name="$(release_id "$install_ref")"
  new_release_dir="${releases_dir}/${release_name}"
  reused_release=0

  need_cmd python3
  set_step "validate python version"
  ensure_supported_python

  mkdir -p "${releases_dir}" "${shared_dir}/data" "${shared_dir}/ssh"
  sync_shared_env "$shared_dir"
  runtime_env_file="${shared_dir}/.env"
  if [[ $FORCE_REINSTALL -eq 0 ]] && release_matches_target "$current_link" "$install_version" "$install_commit"; then
    new_release_dir="$(cd "$current_link" && pwd)"
    reused_release=1
  elif [[ $FORCE_REINSTALL -eq 0 ]] && release_matches_target "$new_release_dir" "$install_version" "$install_commit"; then
    reused_release=1
  else
    rm -rf "$new_release_dir"
    set_step "export release tree"
    export_release_tree "$new_release_dir" "$install_ref"
  fi

  ensure_release_python_runtime "$new_release_dir"

  # DB runtime init must run even when we reuse an existing release tree.
  # Otherwise a previous partial install can leave DB_BACKEND=postgres without
  # POSTGRES_DSN and the service will fail at import-time.
  set_step "load database runtime configuration"
  sqlite_db_path="$(read_env_value SQLITE_DB_PATH "$runtime_env_file")"
  if [[ -z "$sqlite_db_path" ]]; then
    sqlite_db_path="${shared_dir}/data/bot.sqlite3"
  fi
  if [[ $supports_postgres_migration -eq 1 ]]; then
    db_backend="$(read_env_value DB_BACKEND "$runtime_env_file")"
    postgres_dsn="$(read_env_value POSTGRES_DSN "$runtime_env_file")"
    if [[ -z "$db_backend" || "$db_backend" == "sqlite" ]]; then
      db_backend="postgres"
    fi
    if [[ -z "$postgres_dsn" ]]; then
      set_step "auto-provision local postgresql runtime"
      auto_provision_simple_postgres "$runtime_env_file" "$shared_dir" 1
      postgres_dsn="$(read_env_value POSTGRES_DSN "$runtime_env_file")"
      if [[ -z "$postgres_dsn" ]]; then
        echo "POSTGRES_DSN is required for 0.4 installs." >&2
        exit 1
      fi
    fi
    NODE_PLANE_BASE_DIR="${base_dir}" \
    NODE_PLANE_APP_DIR="${new_release_dir}" \
    NODE_PLANE_SHARED_DIR="${shared_dir}" \
    DB_BACKEND="${db_backend}" \
    POSTGRES_DSN="${postgres_dsn}" \
    SQLITE_DB_PATH="${sqlite_db_path}" \
    "${new_release_dir}/.venv/bin/python" "${new_release_dir}/app/manage_db.py" init
    if [[ $reused_release -eq 0 ]]; then
      if [[ -f "$sqlite_db_path" ]]; then
        local migrate_output
        echo "Migrating SQLite data into PostgreSQL..."
        set_step "migrate sqlite to postgresql"
        migrate_output="$(
          NODE_PLANE_BASE_DIR="${base_dir}" \
          NODE_PLANE_APP_DIR="${new_release_dir}" \
          NODE_PLANE_SHARED_DIR="${shared_dir}" \
          DB_BACKEND="${db_backend}" \
          POSTGRES_DSN="${postgres_dsn}" \
          SQLITE_DB_PATH="${sqlite_db_path}" \
          "${new_release_dir}/.venv/bin/python" "${new_release_dir}/app/manage_db.py" migrate-to-postgres --sqlite-path "$sqlite_db_path"
        )"
        printf '%s\n' "$migrate_output"
        if printf '%s\n' "$migrate_output" | grep -q '^MIGRATE|success$'; then
          echo "Verifying PostgreSQL migration..."
          set_step "verify sqlite to postgresql migration"
          NODE_PLANE_BASE_DIR="${base_dir}" \
          NODE_PLANE_APP_DIR="${new_release_dir}" \
          NODE_PLANE_SHARED_DIR="${shared_dir}" \
          DB_BACKEND="${db_backend}" \
          POSTGRES_DSN="${postgres_dsn}" \
          SQLITE_DB_PATH="${sqlite_db_path}" \
          "${new_release_dir}/.venv/bin/python" "${new_release_dir}/app/manage_db.py" verify-migration --sqlite-path "$sqlite_db_path"
        else
          echo "Skipping PostgreSQL verification because legacy SQLite import was not applied."
        fi
      else
        echo "No SQLite source found at ${sqlite_db_path}; skipping SQLite -> PostgreSQL migration."
      fi
    fi
  else
    if [[ $reused_release -eq 0 ]]; then
      echo "Selected ref ${install_ref} uses the legacy SQLite runtime; skipping PostgreSQL provisioning and migration."
    fi
    NODE_PLANE_BASE_DIR="${base_dir}" \
    NODE_PLANE_APP_DIR="${new_release_dir}" \
    NODE_PLANE_SHARED_DIR="${shared_dir}" \
    SQLITE_DB_PATH="${sqlite_db_path}" \
    "${new_release_dir}/.venv/bin/python" "${new_release_dir}/app/manage_db.py" init
  fi

  ln -sfn "$new_release_dir" "$current_link"

  validate_simple_layout "$current_link" "$shared_dir"

  local unit_path="${REPO_ROOT}/scripts/${service_name}.service"
  cat > "$unit_path" <<EOF
[Unit]
Description=Node Plane
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${current_link}
Environment=NODE_PLANE_BASE_DIR=${base_dir}
Environment=NODE_PLANE_APP_DIR=${current_link}
Environment=NODE_PLANE_SHARED_DIR=${shared_dir}
EnvironmentFile=${shared_dir}/.env
ExecStart=${current_link}/.venv/bin/python ${current_link}/app/main.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

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
  echo "Generated systemd unit:"
  echo "  ${unit_path}"
  echo
  if [[ $AUTO_INSTALL_SYSTEMD -eq 1 ]]; then
    install_systemd_unit "$unit_path"
  elif [[ $NON_INTERACTIVE -eq 0 ]]; then
    local answer
    read -r -p "Install the systemd unit to /etc/systemd/system/${service_name}.service now? [y/N]: " answer
    if [[ "${answer:-}" =~ ^[Yy]$ ]]; then
      install_systemd_unit "$unit_path"
    fi
  fi

  if [[ "$AUTO_SETUP_DRIVER_AGENTS_ON_INSTALL" == "1" ]]; then
    echo
    echo "Running post-install driver/agent setup (best-effort)..."
    if ! "${REPO_ROOT}/scripts/setup_driver_agents.sh"; then
      echo "Driver/agent setup reported issues. Continuing because best-effort is enabled." >&2
    fi
  fi

  echo
  echo "First-run path:"
  echo "  1. This checkout has been exported to:"
  echo "     ${new_release_dir}"
  echo "     Shared runtime state lives under ${shared_dir}"
  if [[ $AUTO_INSTALL_SYSTEMD -eq 1 ]]; then
    echo "  2. Service install is done. Verify the host setup:"
  else
    echo "  2. Start the bot service, then verify the host setup:"
    echo "     sudo systemctl enable --now ${service_name}"
  fi
  echo "     ./scripts/healthcheck.sh --mode simple"
  echo "  3. Open the bot from the Telegram account listed in ADMIN_IDS"
  echo "  4. Send /start once"
  echo "     The bot will create the admin profile automatically and show first-run setup"
  echo "  5. Choose: Set up this server"
  echo "  6. Open the new server card and run Probe, then Bootstrap"
}

install_systemd_unit() {
  local unit_path="$1"
  local service_name="node-plane"
  need_cmd sudo
  set_step "install systemd unit"
  sudo cp "$unit_path" "/etc/systemd/system/${service_name}.service"
  sudo systemctl daemon-reload
  if ! sudo systemctl enable --now "${service_name}"; then
    echo
    echo "systemd failed to start ${service_name}.service."
    echo "Inspect these commands:"
    echo "  sudo systemctl status ${service_name} --no-pager"
    echo "  sudo journalctl -xeu ${service_name}"
    exit 1
  fi
  sudo systemctl status "${service_name}" --no-pager || true
}

run_portable_install() {
  set_step "ensure docker is installed"
  install_docker_if_missing
  set_step "ensure docker compose is installed"
  install_docker_compose_if_missing
  local image_repo image_tag current_container current_image_ref current_repo current_tag
  image_repo="$(read_env_value NODE_PLANE_IMAGE_REPO)"
  image_tag="$(read_env_value NODE_PLANE_IMAGE_TAG)"
  current_container=""
  if docker compose version >/dev/null 2>&1; then
    current_container="$(docker compose ps -q node-plane 2>/dev/null | tail -n 1 || true)"
  elif command -v docker-compose >/dev/null 2>&1; then
    current_container="$(docker-compose ps -q node-plane 2>/dev/null | tail -n 1 || true)"
  fi
  current_image_ref=""
  if [[ -n "$current_container" ]]; then
    current_image_ref="$(docker inspect -f '{{.Config.Image}}' "$current_container" 2>/dev/null || true)"
  fi
  current_repo="${current_image_ref%:*}"
  current_tag="${current_image_ref##*:}"
  if [[ "$current_image_ref" == "$current_repo" ]]; then
    current_tag=""
  fi
  if [[ $FORCE_REINSTALL -eq 0 ]] \
    && [[ "${image_repo:-node-plane}" != "node-plane" || "${image_tag:-local}" != "local" ]] \
    && [[ -n "$current_repo" ]] \
    && [[ "$current_repo" == "$image_repo" ]] \
    && [[ "$current_tag" == "$image_tag" ]]; then
    echo
    echo "Portable mode install is already on the target image; skipping container redeploy."
    echo "Configured image:"
    echo "  ${image_repo}:${image_tag}"
    return 0
  fi
  if docker compose version >/dev/null 2>&1; then
    if [[ "${image_repo:-node-plane}" == "node-plane" && "${image_tag:-local}" == "local" ]]; then
      set_step "docker compose build and start"
      docker compose up -d --build
    else
      set_step "docker compose pull and start"
      docker compose pull
      docker compose up -d
    fi
  elif command -v docker-compose >/dev/null 2>&1; then
    if [[ "${image_repo:-node-plane}" == "node-plane" && "${image_tag:-local}" == "local" ]]; then
      set_step "docker-compose build and start"
      docker-compose up -d --build
    else
      set_step "docker-compose pull and start"
      docker-compose pull
      docker-compose up -d
    fi
  else
    echo "Docker Compose is required. Install docker compose plugin or docker-compose." >&2
    exit 1
  fi

  echo
  echo "Portable mode bot container is starting."
  echo
  echo "First-run path:"
  echo "  1. Verify the container-oriented setup:"
  echo "     ./scripts/healthcheck.sh --mode portable"
  echo "  2. Open the bot from the Telegram account listed in ADMIN_IDS"
  echo "  3. Send /start once"
  echo "     The bot will create the admin profile automatically and show first-run setup"
  echo "  4. Choose: Set up over SSH"
  echo "  5. Open Admin -> SSH Key"
  echo "  6. Add the generated public key to a remote server"
  echo "  7. Open the new server card and run Probe, then Bootstrap"
}

choose_mode
case "$MODE" in
  simple|portable) ;;
  *)
    echo "Unsupported mode: ${MODE}. Use simple or portable." >&2
    exit 1
    ;;
esac

configure_env
print_repo_location_note

if [[ "$MODE" == "simple" ]]; then
  run_simple_install
else
  run_portable_install
fi
