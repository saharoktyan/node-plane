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
  if [[ ! -f .env ]]; then
    return 0
  fi
  sed -n "s/^${key}=//p" .env | tail -n 1
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

detect_mode() {
  if [[ "$MODE" != "auto" ]]; then
    return 0
  fi

  if has_cmd docker && { docker compose ps >/dev/null 2>&1 || command -v docker-compose >/dev/null 2>&1; }; then
    if has_cmd docker && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'node-plane'; then
      MODE="portable"
      return 0
    fi
  fi

  if has_cmd systemctl && systemctl list-unit-files node-plane.service >/dev/null 2>&1; then
    MODE="simple"
    return 0
  fi

  if [[ -d .venv ]]; then
    MODE="simple"
    return 0
  fi

  MODE="portable"
}

check_repo_basics() {
  section "Repository"
  check_file_exists "app/main.py" "App entrypoint"
  check_file_exists "requirements.txt" "Requirements file"
  check_file_exists "docker-compose.yml" "Compose file"
  check_file_exists ".env.example" "Environment template"

  if [[ -f .env ]]; then
    ok ".env file exists"
  else
    fail ".env file is missing"
    add_remediation "Create the environment file: cp .env.example .env"
  fi
}

check_env() {
  section "Configuration"

  local bot_token admin_ids base_dir app_dir shared_dir ssh_key
  bot_token="$(read_env_value BOT_TOKEN)"
  admin_ids="$(read_env_value ADMIN_IDS)"
  base_dir="$(read_env_value NODE_PLANE_BASE_DIR)"
  app_dir="$(read_env_value NODE_PLANE_APP_DIR)"
  shared_dir="$(read_env_value NODE_PLANE_SHARED_DIR)"
  ssh_key="$(read_env_value SSH_KEY)"

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

  if has_cmd python3; then
    local version
    version="$(python_version)"
    if python_supported; then
      ok "python3 is available (${version})"
      python_ok=1
    else
      fail "python3 version ${version} is unsupported for the current bot runtime"
      add_remediation "Install Python 3.11.x or 3.12.x on the host and recreate the virtualenv"
    fi
  else
    fail "python3 is missing"
    add_remediation "Install Python 3.11.x or 3.12.x on the host"
  fi

  if has_cmd pip || python3 -m pip --version >/dev/null 2>&1; then
    ok "pip is available"
    pip_ok=1
  else
    fail "pip is missing"
    add_remediation "Install pip for Python 3 on the host"
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

  local runtime_env_file db_backend postgres_dsn sqlite_db_path
  runtime_env_file="${shared_dir}/.env"
  db_backend="$(read_env_value DB_BACKEND "$runtime_env_file")"
  postgres_dsn="$(read_env_value POSTGRES_DSN "$runtime_env_file")"
  sqlite_db_path="$(read_env_value SQLITE_DB_PATH "$runtime_env_file")"
  if [[ -z "$db_backend" ]]; then
    db_backend="postgres"
  fi
  if [[ -z "$sqlite_db_path" ]]; then
    sqlite_db_path="${shared_dir}/data/bot.sqlite3"
  fi

  if [[ "$db_backend" == "postgres" && -n "$postgres_dsn" ]]; then
    ok "PostgreSQL runtime is configured"
  elif [[ "$db_backend" == "sqlite" && -f "$sqlite_db_path" ]]; then
    warn "Legacy SQLite source is still present at ${sqlite_db_path}"
    add_remediation "Run a 0.4 update with POSTGRES_DSN configured to migrate data into PostgreSQL"
  elif [[ "$db_backend" == "sqlite" ]]; then
    warn "Legacy SQLite mode is configured but no SQLite source was found"
    add_remediation "Switch DB_BACKEND to postgres, set POSTGRES_DSN in ${shared_dir}/.env, and rerun installation/update"
  elif [[ "$db_backend" == "postgres" ]]; then
    warn "POSTGRES_DSN is missing for PostgreSQL runtime"
    add_remediation "Set POSTGRES_DSN in ${shared_dir}/.env and rerun installation/update"
  else
    warn "No PostgreSQL configuration or legacy SQLite source was detected"
    add_remediation "Set POSTGRES_DSN in ${shared_dir}/.env and initialize the database: .venv/bin/python app/manage_db.py init"
  fi

  if has_cmd systemctl; then
    if systemctl list-unit-files node-plane.service >/dev/null 2>&1; then
      ok "systemd unit node-plane.service is installed"
      systemd_ok=1
      if systemctl is-active --quiet node-plane.service; then
        ok "node-plane.service is active"
        service_active_ok=1
      else
        warn "node-plane.service is not active"
        add_remediation "Start the service: sudo systemctl enable --now node-plane"
      fi
    else
      warn "systemd unit node-plane.service is not installed"
      add_remediation "Install the unit: ./scripts/install.sh --mode simple --install-systemd"
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
