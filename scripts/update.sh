#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
if [[ -f "${REPO_ROOT}/CONTROLLER_PACKAGE.json" && -z "${NODE_PLANE_SHARED_DIR:-}" ]]; then
  if [[ "$REPO_ROOT" == */current ]]; then
    NODE_PLANE_BASE_DIR="${REPO_ROOT%/current}"
  elif [[ "$(basename "$(dirname "$REPO_ROOT")")" == releases ]]; then
    NODE_PLANE_BASE_DIR="$(dirname "$(dirname "$REPO_ROOT")")"
  fi
  if [[ -n "${NODE_PLANE_BASE_DIR:-}" ]]; then
    NODE_PLANE_SHARED_DIR="${NODE_PLANE_BASE_DIR}/shared"
    NODE_PLANE_APP_DIR="${NODE_PLANE_BASE_DIR}/current"
  fi
fi
source "${SCRIPT_DIR}/postgres_runtime.sh"
source "${SCRIPT_DIR}/python_runtime.sh"
source "${SCRIPT_DIR}/uv_runtime.sh"
source "${SCRIPT_DIR}/lib/stack_update.sh"
source "${SCRIPT_DIR}/lib/controller_archive.sh"
FROM_SOURCE=0

MODE="${MODE:-auto}"
TARGET_BRANCH="${NODE_PLANE_UPDATE_BRANCH:-}"
TARGET_REF=""
SKIP_PULL=0
SKIP_DEPS=0
SKIP_RESTART=0
HEALTH_TIMEOUT=30
CURRENT_STEP="startup"
PYTHON_BIN=""
SIMPLE_BOT_SERVICE="node-plane-telegram.service"
STACK_JOB=""
CORE_ARMED=0
STACK_COMPONENT="backend"

set_step() {
  CURRENT_STEP="$1"
}

on_error() {
  local exit_code="$1"
  echo >&2
  echo "Update failed during step: ${CURRENT_STEP}" >&2
  echo "Failing command: ${BASH_COMMAND}" >&2
  echo "Exit code: ${exit_code}" >&2
  if [[ -n "$STACK_JOB" ]]; then
    stack_progress "$STACK_COMPONENT" failed || true
    stack_progress status failed || true
    stack_progress error_code core_update_failed || true
    if [[ "$CORE_ARMED" == 1 ]]; then
      rollback_simple "$STACK_PREVIOUS_RELEASE" "$STACK_CURRENT_LINK" "$STACK_NEW_RELEASE"
    else
      if [[ -n "${STACK_SNAPSHOT:-}" ]]; then
        if stack_restore; then
          stack_progress rollback_status succeeded || true
          sudo rm -rf "$STACK_SNAPSHOT"
        else
          stack_progress rollback_status failed || true
        fi
      else
        stack_progress rollback_status succeeded || true
      fi
    fi
  fi
}

trap 'on_error $?' ERR
trap cleanup_controller_archive EXIT

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
    --from-source)
      FROM_SOURCE=1
      shift
      ;;
    --skip-pull)
      SKIP_PULL=1
      shift
      ;;
    --stack-job)
      STACK_JOB="${2:-}"
      if [[ ! "$STACK_JOB" =~ ^[0-9a-fA-F-]{36}$ ]]; then
        echo "Invalid stack update job ID" >&2
        exit 2
      fi
      shift 2
      ;;
    --branch)
      TARGET_BRANCH="${2:-}"
      shift 2
      ;;
    --branch=*)
      TARGET_BRANCH="${1#*=}"
      shift
      ;;
    --to)
      TARGET_REF="${2:-}"
      shift 2
      ;;
    --to=*)
      TARGET_REF="${1#*=}"
      shift
      ;;
    --skip-deps)
      SKIP_DEPS=1
      shift
      ;;
    --skip-restart)
      SKIP_RESTART=1
      shift
      ;;
    --health-timeout)
      HEALTH_TIMEOUT="${2:-}"
      shift 2
      ;;
    --health-timeout=*)
      HEALTH_TIMEOUT="${1#*=}"
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage:
  scripts/update.sh [--mode auto|simple|portable] [--branch main|dev] [--to <ref>] [--skip-pull] [--skip-deps] [--skip-restart] [--health-timeout 30]

Modes:
  auto      Detect update mode from local environment
  simple    Update the host/systemd deployment with rollback support
  portable  Unsupported; use systemd/simple mode

Flags:
  --branch           Branch to use as the update source
  --to               Published release tag; Git refs are available with --from-source
  --from-source      Development only: export a Git checkout instead of downloading an archive
  --skip-pull        Skip fetching Git refs in --from-source mode
  --skip-deps        Unsupported in release-based systemd updates.
  --skip-restart     Do not restart the service/container after applying changes
  --health-timeout   Seconds to wait for node-plane-telegram.service to become active after restart
EOF
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [[ "$FROM_SOURCE" == 1 && -n "${NODE_PLANE_SOURCE_DIR:-}" ]]; then
  REPO_ROOT="$NODE_PLANE_SOURCE_DIR"
fi
cd "$REPO_ROOT"

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

read_env_value() {
  local key="$1"
  local file="${2:-.env}"
  if [[ "$file" == .env && -n "${NODE_PLANE_SHARED_DIR:-}" ]]; then file="${NODE_PLANE_SHARED_DIR}/.env"; fi
  if [[ ! -f "$file" ]]; then
    return 0
  fi
  sed -n "s/^${key}=//p" "$file" | tail -n 1
}

set_env_value_in_file() {
  local file="$1"
  local key="$2"
  local value="$3"
  if [[ ! -f "$file" ]]; then
    touch "$file"
  fi
  if grep -q "^${key}=" "$file"; then
    sed -i "s|^${key}=.*$|${key}=${value}|" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

detect_mode() {
  if [[ "$MODE" != "auto" ]]; then
    return 0
  fi

  if has_cmd systemctl && systemctl list-unit-files node-plane-telegram.service >/dev/null 2>&1; then
    MODE="simple"
    return 0
  fi

  if has_cmd docker; then
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'node-plane'; then
      MODE="portable"
      return 0
    fi
  fi

  if [[ -d .venv ]]; then
    MODE="simple"
    return 0
  fi

  MODE="portable"
}

read_version() {
  if [[ -f VERSION ]]; then
    tr -d '\n' < VERSION
  else
    echo "0.1.0"
  fi
}

read_commit() {
  if git rev-parse --short HEAD >/dev/null 2>&1; then
    git rev-parse --short HEAD
  elif [[ -f BUILD_COMMIT ]]; then
    tr -d '\n' < BUILD_COMMIT
  else
    echo "unknown"
  fi
}

print_version() {
  local semver commit
  semver="$(read_version)"
  commit="$(read_commit)"
  if [[ "$commit" == "unknown" ]]; then
    echo "${semver}"
  else
    echo "${semver} · ${commit}"
  fi
}

current_git_commit() {
  local ref="${1:-HEAD}"
  if [[ -n "${CONTROLLER_COMMIT:-}" ]]; then echo "$CONTROLLER_COMMIT"; return; fi
  if git rev-parse --short "$ref" >/dev/null 2>&1; then
    git rev-parse --short "$ref"
  else
    echo "unknown"
  fi
}

read_version_at_ref() {
  local ref="${1:-HEAD}"
  local value
  if [[ -n "${CONTROLLER_VERSION:-}" ]]; then echo "$CONTROLLER_VERSION"; return; fi
  value="$(git show "${ref}:VERSION" 2>/dev/null | tr -d '\n' || true)"
  if [[ -n "$value" ]]; then
    echo "$value"
  else
    read_version
  fi
}

release_id_base() {
  local ref="${1:-HEAD}"
  local semver commit
  semver="$(read_version_at_ref "$ref")"
  commit="$(current_git_commit "$ref")"
  if [[ "$commit" == "unknown" ]]; then
    echo "${semver}"
  else
    echo "${semver}-${commit}"
  fi
}

unique_release_id() {
  local releases_dir="$1"
  local ref="${2:-HEAD}"
  local base candidate suffix
  base="$(release_id_base "$ref")"
  candidate="$base"
  suffix=1
  while [[ -e "${releases_dir}/${candidate}" ]]; do
    candidate="${base}-r${suffix}"
    suffix=$((suffix + 1))
  done
  echo "$candidate"
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
  mkdir -p "$shared_dir"
  # Installed shared state is authoritative: the checkout's bootstrap .env can
  # predate generated PostgreSQL credentials, adapter identity and node targets.
  # Build a private replacement and rename it so worker timer ticks cannot read
  # a truncated environment during update preparation.
  local runtime_env="${shared_dir}/.env" temporary
  temporary="$(mktemp "${shared_dir}/.env-update.XXXXXX")"
  chmod 600 "$temporary"
  if [[ -f "$runtime_env" ]]; then
    cat "$runtime_env" > "$temporary"
  elif [[ -f .env ]]; then
    cat .env > "$temporary"
  else
    rm -f "$temporary"
    echo "No runtime environment found for the update" >&2
    return 1
  fi
  if [[ "${FROM_SOURCE:-1}" == 0 ]]; then
    set_env_value_in_file "$temporary" "NODE_PLANE_SOURCE_DIR" "$(read_env_value NODE_PLANE_APP_DIR "$temporary")"
  else
    set_env_value_in_file "$temporary" "NODE_PLANE_SOURCE_DIR" "$REPO_ROOT"
  fi
  set_env_value_in_file "$temporary" "NODE_PLANE_INSTALL_MODE" "$MODE"
  mv -f "$temporary" "$runtime_env"
}

fetch_code() {
  if [[ "$FROM_SOURCE" == 0 ]]; then
    local archive_branch="${TARGET_BRANCH:-$(read_env_value NODE_PLANE_UPDATE_BRANCH)}"
    set_step "download and verify controller archive"
    prepare_controller_archive "${archive_branch:-main}" "$TARGET_REF"
    TARGET_REF="$CONTROLLER_REF"
    return
  fi
  if [[ $SKIP_PULL -eq 1 ]]; then
    echo "Skipping git fetch"
    return 0
  fi
  need_cmd git
  echo "Fetching git refs..."
  set_step "fetch git refs"
  git fetch --quiet --tags origin
}

resolve_target_ref() {
  if [[ -n "$TARGET_REF" ]]; then
    echo "$TARGET_REF"
    return 0
  fi
  if [[ -n "$TARGET_BRANCH" ]]; then
    echo "origin/${TARGET_BRANCH}"
    return 0
  fi
  echo "HEAD"
}

simple_paths() {
  local base_dir app_dir shared_dir releases_dir current_link
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
  releases_dir="${base_dir}/releases"
  current_link="${base_dir}/current"

  printf '%s\n%s\n%s\n%s\n%s\n' "$base_dir" "$app_dir" "$shared_dir" "$releases_dir" "$current_link"
}

wait_for_service() {
  local timeout="$1"
  local elapsed=0
  local stable=0
  while (( elapsed < timeout )); do
    if sudo systemctl is-active --quiet "$SIMPLE_BOT_SERVICE"; then
      if [[ -z "$STACK_JOB" ]]; then
        return 0
      fi
      if sudo systemctl is-active --quiet node-plane-driver.service node-plane-backend.service node-plane-backend-worker.timer \
          && "${STACK_CURRENT_LINK}/.venv/bin/python" -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8080/health/ready", timeout=2).read()' >/dev/null 2>&1; then
        stable=$((stable + 1))
        if [[ "$stable" -ge 3 ]]; then
          return 0
        fi
      else
        stable=0
      fi
    else
      stable=0
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done
  return 1
}

rollback_simple() {
  trap - ERR
  CORE_ARMED=0
  if [[ -n "$STACK_JOB" ]]; then
    stack_progress "$STACK_COMPONENT" failed
    stack_progress status failed
    stack_progress error_code core_update_failed
  fi
  local previous_release="$1"
  local current_link="$2"
  local failed_release="$3"

  if [[ -z "$previous_release" || ! -d "$previous_release" ]]; then
    echo "No previous release is available for rollback." >&2
    echo "Failed release remains at: ${failed_release}" >&2
    sudo systemctl status "$SIMPLE_BOT_SERVICE" --no-pager || true
    sudo journalctl -u "$SIMPLE_BOT_SERVICE" -n 50 --no-pager || true
    exit 1
  fi

  echo "Rolling back to previous release:"
  echo "  ${previous_release}"
  ln -sfn "$previous_release" "$current_link"
  local restored=1
  if [[ -n "$STACK_JOB" ]]; then
    stack_restore || restored=0
  fi
  sudo systemctl daemon-reload
  sudo systemctl restart "$SIMPLE_BOT_SERVICE"
  if [[ -n "$STACK_JOB" ]]; then
    sudo systemctl restart node-plane-driver.service || restored=0
    sudo systemctl start node-plane-backend-worker.timer || restored=0
  fi
  if sudo systemctl list-unit-files node-plane-backend.service --no-legend 2>/dev/null | grep -q node-plane-backend.service; then
    if [[ -f "${previous_release}/app/backend/http_api.py" ]]; then
      sudo systemctl restart node-plane-backend.service || restored=0
    else
      sudo systemctl stop node-plane-backend-worker.timer node-plane-backend.service || true
    fi
  fi
  if wait_for_service "$HEALTH_TIMEOUT" && [[ "$restored" == 1 ]]; then
    if [[ -n "$STACK_JOB" ]]; then
      stack_progress rollback_status succeeded
      stack_progress status failed
      local component
      for component in backend worker driver telegram; do
        if [[ "$component" != "$STACK_COMPONENT" ]]; then
          stack_progress "$component" rolled_back
        fi
      done
      sudo rm -rf "$STACK_SNAPSHOT"
    fi
    echo "Rollback completed."
    echo "Failed release remains at: ${failed_release}"
    exit 1
  fi

  echo "Rollback failed. Inspect the service manually." >&2
  if [[ -n "$STACK_JOB" ]]; then
    stack_progress rollback_status failed
    stack_progress status failed
  fi
  sudo systemctl status "$SIMPLE_BOT_SERVICE" --no-pager || true
  sudo journalctl -u "$SIMPLE_BOT_SERVICE" -n 80 --no-pager || true
  exit 1
}

update_simple() {
  need_cmd sudo
  if [[ $SKIP_DEPS -eq 1 ]]; then
    echo "--skip-deps is not supported in simple mode with release-based updates." >&2
    exit 1
  fi

  local base_dir app_dir shared_dir releases_dir current_link
  mapfile -t _paths < <(simple_paths)
  base_dir="${_paths[0]}"
  app_dir="${_paths[1]}"
  shared_dir="${_paths[2]}"
  releases_dir="${_paths[3]}"
  current_link="${_paths[4]}"
  PYTHON_BIN="$(select_controller_python "$shared_dir")"
  echo "Using Python runtime: ${PYTHON_BIN}"
  local runtime_env_file db_backend postgres_dsn
  runtime_env_file="${shared_dir}/.env"
  if [[ -n "$STACK_JOB" ]]; then
    STACK_PROGRESS_FILE="${shared_dir}/data/updates/${STACK_JOB}.json"
    stack_progress status running
    stack_progress backend running
    stack_snapshot "$runtime_env_file"
  fi

  local previous_release new_release_name new_release_dir
  local target_ref
  previous_release="$(readlink -f "$current_link" 2>/dev/null || true)"
  STACK_PREVIOUS_RELEASE="$previous_release"
  STACK_CURRENT_LINK="$current_link"
  target_ref="$(resolve_target_ref)"
  new_release_name="$(unique_release_id "$releases_dir" "$target_ref")"
  new_release_dir="${releases_dir}/${new_release_name}"
  STACK_NEW_RELEASE="$new_release_dir"

  mkdir -p "$releases_dir" "${shared_dir}/data" "${shared_dir}/ssh"
  sync_shared_env "$shared_dir"

  set_step "export release tree"
  echo "Preparing new release:"
  echo "  ${new_release_dir}"
  echo "From ref:"
  echo "  ${target_ref}"
  export_release_tree "$new_release_dir" "$target_ref"

  echo "Installing Python runtime for new release..."
  set_step "install python dependencies with uv"
  install_release_dependencies "$new_release_dir" "$shared_dir"

  echo "Applying database/schema init..."
  set_step "load database runtime configuration"
  db_backend="$(read_env_value DB_BACKEND "$runtime_env_file")"
  postgres_dsn="$(read_env_value POSTGRES_DSN "$runtime_env_file")"
  if [[ -z "$db_backend" ]]; then
    db_backend="postgres"
  fi
  if [[ "$db_backend" != "postgres" ]]; then
    echo "Only PostgreSQL runtime is supported." >&2
    exit 1
  fi
  if [[ "$db_backend" == "postgres" ]]; then
    set_step "auto-provision local postgresql runtime"
    auto_provision_simple_postgres "$runtime_env_file" "$shared_dir" 1
    postgres_dsn="$(read_env_value POSTGRES_DSN "$runtime_env_file")"
    if [[ -z "$postgres_dsn" ]]; then
      echo "POSTGRES_DSN is required for 0.4 runtime updates." >&2
      exit 1
    fi
  fi

  if [[ -f "${new_release_dir}/app/backend/admin_cli.py" ]]; then
    set_step "initialize backend schema"
    NODE_PLANE_BASE_DIR="${base_dir}" \
    NODE_PLANE_APP_DIR="${new_release_dir}" \
    NODE_PLANE_SHARED_DIR="${shared_dir}" \
    DB_BACKEND="postgres" \
    POSTGRES_DSN="${postgres_dsn}" \
    PYTHONPATH="${new_release_dir}/app" \
    "${new_release_dir}/.venv/bin/python" -m backend.admin_cli init-schema
  fi

  if [[ $SKIP_RESTART -eq 1 ]]; then
    echo "Skipping service restart"
    echo "Release is prepared but not activated:"
    echo "  ${new_release_dir}"
    exit 0
  fi

  if [[ -n "$STACK_JOB" ]]; then
    STACK_COMPONENT=worker
    stack_progress worker running
    NODE_PLANE_APP_DIR="$new_release_dir" NODE_PLANE_SHARED_DIR="$shared_dir" \
      PYTHONPATH="${new_release_dir}/app" "${new_release_dir}/.venv/bin/python" \
      -c 'import backend.executor; import telegram_client.main'
    sudo systemctl stop node-plane-backend-worker.timer
    CORE_ARMED=1
  fi

  echo "Switching current release..."
  set_step "activate new release"
  ln -sfn "$new_release_dir" "$current_link"
  if [[ -n "$STACK_JOB" ]]; then
    STACK_COMPONENT=driver
    stack_progress driver running
    NODE_PLANE_BASE_DIR="$base_dir" NODE_PLANE_APP_DIR="$current_link" \
      NODE_PLANE_SHARED_DIR="$shared_dir" NODE_PLANE_INSTALL_RUST=no \
      bash "${current_link}/scripts/setup_driver_agents.sh" --skip-agents --strict --bin-source release
    NODE_PLANE_APP_DIR="$current_link" NODE_PLANE_SHARED_DIR="$shared_dir" \
      PYTHONPATH="${current_link}/app" "${current_link}/.venv/bin/python" - <<'PYDRIVER'
import grpc
from backend.driver_transport import GrpcIntentDriver
from config import APP_COMMIT
from config import NODE_DRIVER_GRPC_TARGET
with grpc.insecure_channel(NODE_DRIVER_GRPC_TARGET) as channel:
    actual = GrpcIntentDriver(channel).binary_info().get('commit') or ''
if len(actual) < 7 or APP_COMMIT == 'unknown' or not (APP_COMMIT.startswith(actual) or actual.startswith(APP_COMMIT)):
    raise SystemExit('Controller driver version verification failed')
PYDRIVER
    stack_progress driver succeeded
    STACK_COMPONENT=backend
  fi

  # Fallback: if shared env still has DB_BACKEND=postgres without POSTGRES_DSN,
  # auto-provision PostgreSQL runtime before service restart.
  db_backend="$(read_env_value DB_BACKEND "$runtime_env_file")"
  postgres_dsn="$(read_env_value POSTGRES_DSN "$runtime_env_file")"
  if [[ -z "$db_backend" ]]; then
    db_backend="postgres"
  fi
  if [[ "$db_backend" == "postgres" ]]; then
    set_step "fallback auto-provision local postgresql runtime"
    auto_provision_simple_postgres "$runtime_env_file" "$shared_dir" 1
    postgres_dsn="$(read_env_value POSTGRES_DSN "$runtime_env_file")"
    if [[ -z "$postgres_dsn" ]]; then
      echo "POSTGRES_DSN is required for 0.4 runtime updates." >&2
      exit 1
    fi
    NODE_PLANE_BASE_DIR="${base_dir}" \
    NODE_PLANE_APP_DIR="${new_release_dir}" \
    NODE_PLANE_SHARED_DIR="${shared_dir}" \
    DB_BACKEND="${db_backend}" \
    POSTGRES_DSN="${postgres_dsn}" \
    PYTHONPATH="${new_release_dir}/app" \
    "${new_release_dir}/.venv/bin/python" -m backend.admin_cli init-schema
  fi

  if [[ -f "${new_release_dir}/scripts/install_backend_systemd.sh" ]]; then
    set_step "install backend services"
    if ! sudo bash "${new_release_dir}/scripts/install_backend_systemd.sh" "$base_dir" "$shared_dir"; then
      rollback_simple "$previous_release" "$current_link" "$new_release_dir"
    fi
  fi

  echo "Restarting ${SIMPLE_BOT_SERVICE}..."
  if [[ -n "$STACK_JOB" ]]; then
    stack_progress backend succeeded
    stack_progress worker succeeded
    STACK_COMPONENT=telegram
    stack_progress telegram running
  fi
  set_step "restart ${SIMPLE_BOT_SERVICE}"
  sudo systemctl daemon-reload
  if ! sudo systemctl restart "$SIMPLE_BOT_SERVICE"; then
    echo "Service restart failed immediately."
    rollback_simple "$previous_release" "$current_link" "$new_release_dir"
  fi

  if wait_for_service "$HEALTH_TIMEOUT"; then
    if [[ -n "$STACK_JOB" ]]; then
      stack_progress telegram succeeded
      stack_progress status succeeded
      CORE_ARMED=0
      sudo rm -rf "$STACK_SNAPSHOT"
    fi
    echo "New release is healthy."
    sudo systemctl status "$SIMPLE_BOT_SERVICE" --no-pager || true
    retain_successful_releases "$base_dir" "$new_release_dir" "$previous_release" "$shared_dir"
    return 0
  fi

  echo "Updated release did not become healthy within ${HEALTH_TIMEOUT}s."
  set_step "wait for ${SIMPLE_BOT_SERVICE} health"
  sudo journalctl -u "$SIMPLE_BOT_SERVICE" -n 50 --no-pager || true
  rollback_simple "$previous_release" "$current_link" "$new_release_dir"
}

main() {
  detect_mode
  # Download errors also need a durable receipt, before any host change.
  if [[ -n "$STACK_JOB" ]]; then
    mapfile -t _initial_paths < <(simple_paths)
    STACK_PROGRESS_FILE="${_initial_paths[2]}/data/updates/${STACK_JOB}.json"
    stack_progress status running
    stack_progress backend running
  fi
  echo "Detected update mode: ${MODE}"
  echo "Current checkout version: $(print_version)"
  fetch_code
  echo "Source checkout version: $(print_version)"

  case "$MODE" in
    simple)
      update_simple
      echo "Backend nodes use their own agent rollout; refresh changed nodes from the Telegram Updates screen."
      ;;
    portable)
      echo "Portable Docker updates are temporarily unsupported; use a systemd installation." >&2
      return 1
      ;;
    *)
      echo "Unsupported mode: ${MODE}" >&2
      exit 1
      ;;
  esac

  echo
  echo "Update complete."
  echo "Version: $(print_version)"
}

main "$@"
