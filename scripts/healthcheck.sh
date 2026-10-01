#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

MODE="${MODE:-auto}"
FAILURES=0
WARNINGS=0
SIMPLE_LOCAL_READY=0
PORTABLE_REMOTE_READY=0
declare -a REMEDIATIONS=()

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
    -h|--help)
      cat <<'EOF'
Usage:
  scripts/healthcheck.sh [--mode auto|simple|portable]

Modes:
  auto      Detect mode from local environment
  simple    Check host/systemd-oriented setup
  portable  Check Docker-oriented setup
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

color_enabled=0
if [[ -t 1 ]] && command -v tput >/dev/null 2>&1; then
  if [[ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]]; then
    color_enabled=1
  fi
fi

if [[ $color_enabled -eq 1 ]]; then
  RED="$(tput setaf 1)"
  YELLOW="$(tput setaf 3)"
  GREEN="$(tput setaf 2)"
  BLUE="$(tput setaf 4)"
  BOLD="$(tput bold)"
  RESET="$(tput sgr0)"
else
  RED=""
  YELLOW=""
  GREEN=""
  BLUE=""
  BOLD=""
  RESET=""
fi

ok() {
  printf '%s[OK]%s %s\n' "$GREEN" "$RESET" "$1"
}

warn() {
  WARNINGS=$((WARNINGS + 1))
  printf '%s[WARN]%s %s\n' "$YELLOW" "$RESET" "$1"
}

fail() {
  FAILURES=$((FAILURES + 1))
  printf '%s[FAIL]%s %s\n' "$RED" "$RESET" "$1"
}

info() {
  printf '%s%s%s\n' "$BLUE" "$1" "$RESET"
}

add_remediation() {
  local message="$1"
  local existing
  for existing in "${REMEDIATIONS[@]:-}"; do
    if [[ "$existing" == "$message" ]]; then
      return 0
    fi
  done
  REMEDIATIONS+=("$message")
}

section() {
  printf '\n%s%s%s\n' "$BOLD" "$1" "$RESET"
}

has_cmd() {
  command -v "$1" >/dev/null 2>&1
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

read_env_value() {
  local key="$1"
  local file="${2:-${ENV_FILE:-.env}}"
  if [[ ! -f "$file" ]]; then
    return 0
  fi
  sed -n "s/^${key}=//p" "$file" | tail -n 1
}

check_file_exists() {
  local path="$1"
  local label="$2"
  if [[ -e "$path" ]]; then
    ok "${label}: ${path}"
  else
    fail "${label} is missing: ${path}"
  fi
}

systemd_unit_installed() {
  has_cmd systemctl && [[ "$(systemctl show node-plane-telegram.service --property=LoadState --value 2>/dev/null || true)" == "loaded" ]]
}

detect_mode() {
  if [[ "$MODE" != "auto" ]]; then
    return 0
  fi
  # Prefer the explicitly configured host installation over node containers.
  if systemd_unit_installed || [[ -d .venv ]] || [[ -n "$(read_env_value NODE_PLANE_APP_DIR)" ]]; then
    MODE="simple"
  elif has_cmd docker && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'node-plane'; then
    MODE="portable"
  else
    MODE="simple"
  fi
}

check_repo_basics() {
  section "Repository"
  check_file_exists "app/telegram_client/main.py" "Telegram client entrypoint"
  check_file_exists "requirements.txt" "Requirements file"
  if [[ "$MODE" == "portable" ]]; then
    check_file_exists "docker-compose.yml" "Compose file"
  fi
  check_file_exists ".env.example" "Environment template"

  if [[ -f "$ENV_FILE" ]]; then
    ok "Environment file exists: $ENV_FILE"
  else
    fail ".env file is missing"
    add_remediation "Create the environment file: cp .env.example .env"
  fi
}

check_env() {
  section "Configuration"

  local bot_token admin_ids base_dir app_dir shared_dir ssh_key adapter_token_file
  bot_token="$(read_env_value BOT_TOKEN)"
  admin_ids="$(read_env_value ADMIN_IDS)"
  base_dir="$(read_env_value NODE_PLANE_BASE_DIR)"
  app_dir="$(read_env_value NODE_PLANE_APP_DIR)"
  shared_dir="$(read_env_value NODE_PLANE_SHARED_DIR)"
  ssh_key="$(read_env_value SSH_KEY)"
  adapter_token_file="$(read_env_value NODE_PLANE_BACKEND_ADAPTER_TOKEN_FILE)"

  if [[ -n "$bot_token" && "$bot_token" != "replace_me" ]]; then
    ok "BOT_TOKEN is configured"
  else
    fail "BOT_TOKEN is missing or still set to placeholder"
    add_remediation "Set BOT_TOKEN in .env"
  fi

  if [[ -n "$admin_ids" && "$admin_ids" != "123456789" ]]; then
    ok "ADMIN_IDS is configured"
  else
    fail "ADMIN_IDS is missing or still set to placeholder"
    add_remediation "Set ADMIN_IDS in .env to your Telegram numeric user id"
  fi

  if [[ -n "$adapter_token_file" && -f "$adapter_token_file" && -r "$adapter_token_file" ]]; then
    ok "backend adapter credential is available"
  else
    fail "backend adapter credential is missing or unreadable"
    add_remediation "Rerun ./scripts/install.sh --mode simple to create the aiogram adapter credential"
  fi

  if [[ "$MODE" == "simple" ]]; then
    if [[ -z "$app_dir" ]]; then
      app_dir="$base_dir"
    fi
    if [[ -n "$app_dir" ]]; then
      ok "NODE_PLANE_APP_DIR is set to ${app_dir}"
    else
      warn "NODE_PLANE_APP_DIR is not set"
      add_remediation "Set NODE_PLANE_APP_DIR in .env for simple mode"
    fi
    if [[ -n "$shared_dir" ]]; then
      ok "NODE_PLANE_SHARED_DIR is set to ${shared_dir}"
    else
      warn "NODE_PLANE_SHARED_DIR is not set"
      add_remediation "Set NODE_PLANE_SHARED_DIR in .env for runtime data and SSH keys"
    fi
  else
    if [[ -n "$ssh_key" ]]; then
      ok "SSH_KEY is set to ${ssh_key}"
    else
      warn "SSH_KEY is not set"
      add_remediation "Set SSH_KEY in .env if the bot will manage remote nodes over SSH"
    fi
  fi
}

check_simple_mode() {
  section "Simple Mode"

  local python_ok=0
  local pip_ok=0
  local venv_ok=0
  local systemd_ok=0
  local service_active_ok=0
  local docker_ok=0
  local tun_ok=0
  local shared_data_ok=0
  local shared_ssh_ok=0
  local current_link_ok=0
  local app_dir shared_dir base_dir current_target

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
  current_target="$(readlink -f "$app_dir" 2>/dev/null || true)"

  local runtime_python="${app_dir}/.venv/bin/python"
  if [[ ! -x "$runtime_python" ]]; then
    runtime_python="$(select_python_runtime 2>/dev/null || true)"
  fi
  if [[ -n "$runtime_python" ]] && "$runtime_python" -c 'import sys; sys.exit(0 if sys.version_info[:2] in {(3,11),(3,12)} else 1)' >/dev/null 2>&1; then
    ok "Bot Python runtime is supported ($runtime_python)"
    python_ok=1
  else
    fail "No supported bot Python runtime found"
    add_remediation "Install Python 3.11/3.12 and rerun ./scripts/install.sh --mode simple"
  fi
  if [[ -n "$runtime_python" ]] && "$runtime_python" -m pip --version >/dev/null 2>&1; then
    ok "pip is available in the bot runtime"
    pip_ok=1
  else
    fail "pip is missing in the bot runtime"
    add_remediation "Recreate the bot virtualenv with ./scripts/install.sh --mode simple"
  fi

  if [[ -n "$app_dir" && -x "${app_dir}/.venv/bin/python" ]]; then
    ok "virtualenv is present at ${app_dir}/.venv"
    venv_ok=1
  else
    fail "virtualenv is missing or incomplete"
    add_remediation "Prepare the host install: ./scripts/install.sh --mode simple"
  fi

  if [[ -L "$app_dir" && -n "$current_target" && -d "$current_target" ]]; then
    ok "current release symlink points to ${current_target}"
    current_link_ok=1
  elif [[ -d "$app_dir" ]]; then
    warn "NODE_PLANE_APP_DIR exists but is not a release symlink"
    add_remediation "Rerun ./scripts/install.sh --mode simple to switch to the release-based layout"
  else
    warn "current release symlink is missing"
    add_remediation "Rerun ./scripts/install.sh --mode simple to create releases/current/shared layout"
  fi

  if [[ -f "${shared_dir}/.env" ]]; then
    ok "shared environment file exists at ${shared_dir}/.env"
  else
    warn "shared environment file is missing"
    add_remediation "Sync the environment file to shared storage: cp .env ${shared_dir}/.env"
  fi

  if [[ -n "$shared_dir" && -d "${shared_dir}/data" ]]; then
    ok "shared data directory exists at ${shared_dir}/data"
    shared_data_ok=1
  else
    warn "shared data directory is missing"
    add_remediation "Create runtime directories under NODE_PLANE_SHARED_DIR: mkdir -p ${shared_dir}/data ${shared_dir}/ssh"
  fi

  if [[ -n "$shared_dir" && -d "${shared_dir}/ssh" ]]; then
    ok "shared ssh directory exists at ${shared_dir}/ssh"
    shared_ssh_ok=1
  else
    warn "shared ssh directory is missing"
    add_remediation "Create runtime directories under NODE_PLANE_SHARED_DIR: mkdir -p ${shared_dir}/data ${shared_dir}/ssh"
  fi

  local runtime_env_file db_backend postgres_dsn
  runtime_env_file="${shared_dir}/.env"
  db_backend="$(read_env_value DB_BACKEND "$runtime_env_file")"
  postgres_dsn="$(read_env_value POSTGRES_DSN "$runtime_env_file")"
  if [[ -z "$db_backend" ]]; then
    db_backend="postgres"
  fi

  if [[ "$db_backend" == "postgres" && -n "$postgres_dsn" ]]; then
    ok "PostgreSQL runtime is configured"
  elif [[ "$db_backend" == "postgres" ]]; then
    warn "POSTGRES_DSN is missing for PostgreSQL runtime"
    add_remediation "Set POSTGRES_DSN in ${shared_dir}/.env and rerun installation/update"
  else
    warn "No PostgreSQL configuration was detected"
    add_remediation "Set POSTGRES_DSN in ${shared_dir}/.env and initialize the database: .venv/bin/python app/manage_db.py init"
  fi

  if has_cmd systemctl; then
    local bot_service="node-plane-telegram.service"
    if systemd_unit_installed; then
      ok "systemd bot unit is installed"
      systemd_ok=1
      if systemctl is-active --quiet "$bot_service"; then
        ok "${bot_service} is active"
        service_active_ok=1
      else
        warn "${bot_service} is not active"
        add_remediation "Start the selected bot service with systemctl after checking its configuration"
      fi
    else
      warn "systemd unit node-plane-telegram.service is not installed"
      add_remediation "Install the unit: ./scripts/install.sh --mode simple --install-systemd"
    fi
    if [[ -f "${app_dir}/app/backend/http_api.py" ]]; then
      if systemctl is-active --quiet node-plane-backend.service; then
        ok "node-plane-backend.service is active"
      else
        warn "node-plane-backend.service is not active"
        add_remediation "Rerun ./scripts/install.sh --mode simple --install-systemd, then inspect sudo journalctl -u node-plane-backend"
      fi
      if systemctl is-active --quiet node-plane-backend-worker.timer; then
        ok "node-plane-backend-worker.timer is active"
      else
        warn "node-plane-backend-worker.timer is not active"
        add_remediation "Start the worker timer: sudo systemctl enable --now node-plane-backend-worker.timer"
      fi
      if [[ -x "${app_dir}/.venv/bin/python" ]] && "${app_dir}/.venv/bin/python" -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8080/health/ready", timeout=3).read()' >/dev/null 2>&1; then
        ok "backend API is ready on loopback"
      else
        warn "backend API readiness check failed"
        add_remediation "Inspect sudo journalctl -u node-plane-backend and verify backend schema initialization"
      fi
    fi
  else
    warn "systemctl is unavailable on this host"
    add_remediation "Use simple mode on a systemd-based Linux host"
  fi

  if has_cmd docker; then
    if docker info >/dev/null 2>&1; then
      ok "Docker daemon is reachable"
      docker_ok=1
    else
      warn "docker exists but daemon is not reachable for the current user"
      add_remediation "Ensure Docker is installed and the current user can access the daemon"
    fi
  else
    warn "docker is not installed"
    add_remediation "Rerun install/update; Node Plane can auto-install Docker when it needs PostgreSQL runtime provisioning"
  fi

  if [[ -c /dev/net/tun ]]; then
    ok "/dev/net/tun is available"
    tun_ok=1
  else
    warn "/dev/net/tun is missing"
    add_remediation "Use a VPS/kernel setup that provides /dev/net/tun for TUN-based runtime support"
  fi

  if [[ $python_ok -eq 1 && $pip_ok -eq 1 && $venv_ok -eq 1 && $systemd_ok -eq 1 && $service_active_ok -eq 1 && $docker_ok -eq 1 && $tun_ok -eq 1 && $shared_data_ok -eq 1 && $shared_ssh_ok -eq 1 && $current_link_ok -eq 1 ]]; then
    SIMPLE_LOCAL_READY=1
  fi
}

check_portable_mode() {
  section "Portable Mode"

  local docker_ok=0
  local compose_ok=0
  local container_ok=0
  local ssh_dir_ok=0

  if has_cmd docker; then
    ok "docker is available"
  else
    fail "docker is missing"
    add_remediation "Install Docker on the bot host"
    return 0
  fi

  if docker info >/dev/null 2>&1; then
    ok "Docker daemon is reachable"
    docker_ok=1
  else
    fail "Docker daemon is not reachable"
    add_remediation "Start Docker and ensure the current user can access the daemon"
  fi

  if docker compose version >/dev/null 2>&1; then
    ok "docker compose plugin is available"
    compose_ok=1
  elif has_cmd docker-compose; then
    ok "docker-compose is available"
    compose_ok=1
  else
    fail "Docker Compose is missing"
    add_remediation "Install the docker compose plugin or docker-compose"
  fi

  if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'node-plane'; then
    ok "node-plane container is running"
    container_ok=1
  else
    warn "node-plane container is not running"
    add_remediation "Start the bot container: ./scripts/install.sh --mode portable"
  fi

  if [[ -d data ]]; then
    ok "data/ directory exists"
  else
    warn "data/ directory is missing"
    add_remediation "Create runtime directories: mkdir -p data ssh"
  fi

  if [[ -d ssh ]]; then
    ok "ssh/ directory exists"
    ssh_dir_ok=1
  else
    warn "ssh/ directory is missing"
    add_remediation "Create the ssh/ directory: mkdir -p ssh"
  fi

  if [[ $docker_ok -eq 1 && $compose_ok -eq 1 && $container_ok -eq 1 && $ssh_dir_ok -eq 1 ]]; then
    PORTABLE_REMOTE_READY=1
  fi
}

print_mode_readiness() {
  section "Readiness"
  if [[ "$MODE" == "simple" ]]; then
    if [[ $SIMPLE_LOCAL_READY -eq 1 ]]; then
      ok "This host is ready for Simple Mode and local node deployment"
      info "Next: open the bot as admin, send /start, choose 'Set up this server', then run Probe and Bootstrap"
    else
      warn "This host is not fully ready for local node deployment yet"
      info "Goal: active systemd service, reachable Docker daemon, and /dev/net/tun on the same host"
    fi
  else
    if [[ $PORTABLE_REMOTE_READY -eq 1 ]]; then
      ok "This host is ready for Portable Mode and remote SSH-managed nodes"
      info "Next: open the bot as admin, send /start, choose 'Set up over SSH', add the SSH key, then run Probe and Bootstrap"
    else
      warn "This host is not fully ready for Portable Mode yet"
      info "Goal: running bot container, working Docker Compose, and ssh/ available for generated keys"
    fi
  fi
}

print_summary() {
  section "Summary"
  info "Mode: ${MODE}"
  info "Failures: ${FAILURES}"
  info "Warnings: ${WARNINGS}"
  if [[ $FAILURES -eq 0 && $WARNINGS -eq 0 ]]; then
    ok "Setup looks healthy"
  elif [[ $FAILURES -eq 0 ]]; then
    warn "Setup is usable, but there are follow-up items"
  else
    fail "Setup is not ready yet"
  fi
}

print_remediations() {
  if [[ ${#REMEDIATIONS[@]} -eq 0 ]]; then
    return 0
  fi
  section "Suggested Fixes"
  local item
  for item in "${REMEDIATIONS[@]}"; do
    printf -- '- %s\n' "$item"
  done
}

source "${SCRIPT_DIR}/python_runtime.sh"
ENV_FILE="${NODE_PLANE_SHARED_DIR:+${NODE_PLANE_SHARED_DIR}/.env}"
if [[ -z "$ENV_FILE" || ! -f "$ENV_FILE" ]]; then
  ENV_FILE="${REPO_ROOT}/.env"
  configured_shared="$(read_env_value NODE_PLANE_SHARED_DIR)"
  if [[ -n "$configured_shared" && -f "${configured_shared}/.env" ]]; then
    ENV_FILE="${configured_shared}/.env"
  fi
fi

detect_mode
case "$MODE" in
  simple|portable) ;;
  *)
    echo "Unsupported mode: ${MODE}. Use auto, simple, or portable." >&2
    exit 1
    ;;
esac

check_repo_basics
check_env

if [[ "$MODE" == "simple" ]]; then
  check_simple_mode
else
  check_portable_mode
fi

print_mode_readiness
print_summary
print_remediations

if [[ $FAILURES -gt 0 ]]; then
  exit 1
fi
