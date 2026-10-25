#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

MODE="${MODE:-auto}"
FAILURES=0
WARNINGS=0
SIMPLE_LOCAL_READY=0
declare -a REMEDIATIONS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      [[ $# -ge 2 ]] || { echo "--mode requires a value" >&2; exit 1; }
      MODE="$2"
      shift 2
      ;;
    --mode=*)
      MODE="${1#*=}"
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage:
  scripts/healthcheck.sh [--mode auto|simple]

Modes:
  auto      Use the supported host/systemd setup
  simple    Check host/systemd-oriented setup
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

# Read values as data: never execute a private environment file as shell code.
read_env_value() {
  local key="$1" file="${2:-${ENV_FILE:-.env}}" value
  [[ -r "$file" ]] || return 0
  value="$(sed -n "s/^[[:space:]]*${key}=//p" "$file" | tail -n 1)"
  value="${value%$'\r'}"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  if [[ "$value" == \"*\" || "$value" == \'*\' ]]; then
    value="${value:1:${#value}-2}"
  fi
  printf '%s' "$value"
}

infer_install_base() {
  local app_root="$1" physical_root
  physical_root="$(cd "$app_root" 2>/dev/null && pwd -P)" || return 0
  if [[ "$(basename "$app_root")" == current && -L "$app_root" ]]; then
    dirname "$app_root"
  elif [[ "$(basename "$(dirname "$physical_root")")" == releases ]]; then
    dirname "$(dirname "$physical_root")"
  fi
}

resolve_layout() {
  local bootstrap_env="${REPO_ROOT}/.env" inferred_base configured_shared initial_base
  inferred_base="$(infer_install_base "$REPO_ROOT")"
  BASE_DIR="${NODE_PLANE_BASE_DIR:-$(read_env_value NODE_PLANE_BASE_DIR "$bootstrap_env")}"
  APP_DIR="${NODE_PLANE_APP_DIR:-$(read_env_value NODE_PLANE_APP_DIR "$bootstrap_env")}"
  if [[ -z "$BASE_DIR" && -n "$APP_DIR" ]]; then
    BASE_DIR="$(infer_install_base "$APP_DIR")"
  fi
  BASE_DIR="${BASE_DIR:-${inferred_base:-$REPO_ROOT}}"
  initial_base="$BASE_DIR"
  APP_DIR="${APP_DIR:-${BASE_DIR}/current}"
  configured_shared="${NODE_PLANE_SHARED_DIR:-$(read_env_value NODE_PLANE_SHARED_DIR "$bootstrap_env")}"
  SHARED_DIR="${configured_shared:-${BASE_DIR}/shared}"
  ENV_FILE="${SHARED_DIR}/.env"
  # A source checkout can contain the installer configuration, pointing to shared.
  # An installed release must never fall back to an unrelated checkout .env.
  if [[ ! -f "$ENV_FILE" && -z "$inferred_base" && -z "${NODE_PLANE_SHARED_DIR:-}" && -f "$bootstrap_env" ]]; then
    ENV_FILE="$bootstrap_env"
  fi
  BASE_DIR="${NODE_PLANE_BASE_DIR:-$(read_env_value NODE_PLANE_BASE_DIR "$ENV_FILE")}"
  BASE_DIR="${BASE_DIR:-$initial_base}"
  APP_DIR="${NODE_PLANE_APP_DIR:-$(read_env_value NODE_PLANE_APP_DIR "$ENV_FILE")}"
  APP_DIR="${APP_DIR:-${BASE_DIR}/current}"
  SHARED_DIR="${NODE_PLANE_SHARED_DIR:-$(read_env_value NODE_PLANE_SHARED_DIR "$ENV_FILE")}"
  SHARED_DIR="${SHARED_DIR:-${BASE_DIR}/shared}"
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
  if [[ "$MODE" == auto ]]; then
    MODE=simple
  fi
}

check_repo_basics() {
  section "Installation files"
  local check_root="$REPO_ROOT"
  [[ ! -d "$APP_DIR" ]] || check_root="$APP_DIR"
  check_file_exists "${check_root}/app/telegram_client/main.py" "Telegram client entrypoint"
  if [[ -f "${check_root}/pyproject.toml" || -f "${check_root}/uv.lock" ]]; then
    check_file_exists "${check_root}/pyproject.toml" "Python project manifest"
    check_file_exists "${check_root}/uv.lock" "Python dependency lock"
  else
    check_file_exists "${check_root}/requirements.txt" "Legacy Python requirements"
  fi
  check_file_exists "${check_root}/.env.example" "Environment template"
  if [[ -r "$ENV_FILE" ]]; then
    ok "Environment file is readable: $ENV_FILE"
  else
    fail "Environment file is missing or unreadable: $ENV_FILE"
    add_remediation "Check the installed configuration at $ENV_FILE; do not create a separate .env inside a release"
  fi
}

check_env() {
  section "Configuration"

  local bot_token admin_ids adapter_token_file
  bot_token="$(read_env_value BOT_TOKEN)"
  admin_ids="$(read_env_value ADMIN_IDS)"
  adapter_token_file="$(read_env_value NODE_PLANE_BACKEND_ADAPTER_TOKEN_FILE)"

  if [[ -n "$bot_token" && "$bot_token" != "replace_me" ]]; then
    ok "BOT_TOKEN is configured"
  else
    fail "BOT_TOKEN is missing or still set to placeholder"
    add_remediation "Set BOT_TOKEN in $ENV_FILE"
  fi

  if [[ -n "$admin_ids" && "$admin_ids" != "123456789" ]]; then
    ok "ADMIN_IDS is configured"
  else
    fail "ADMIN_IDS is missing or still set to placeholder"
    add_remediation "Set ADMIN_IDS in $ENV_FILE to your Telegram numeric user id"
  fi

  if [[ -n "$adapter_token_file" && -f "$adapter_token_file" && -r "$adapter_token_file" ]]; then
    ok "backend adapter credential is available"
  else
    fail "backend adapter credential is missing or unreadable"
    add_remediation "Rerun ./scripts/install.sh --mode simple to create the aiogram adapter credential"
  fi

  ok "Application directory: $APP_DIR"
  ok "Shared directory: $SHARED_DIR"
}

check_simple_mode() {
  section "Simple Mode"

  local python_ok=0
  local dependencies_ok=0
  local venv_ok=0
  local systemd_ok=0
  local service_active_ok=0
  local docker_ok=0
  local tun_ok=0
  local shared_data_ok=0
  local shared_ssh_ok=0
  local current_link_ok=0
  local app_dir shared_dir base_dir current_target

  base_dir="$BASE_DIR"
  app_dir="$APP_DIR"
  shared_dir="$SHARED_DIR"
  current_target="$(readlink -f "$app_dir" 2>/dev/null || true)"

  local runtime_python="${app_dir}/.venv/bin/python"
  if [[ -x "$runtime_python" ]] && "$runtime_python" -I -c 'import sys; sys.exit(0 if sys.version_info[:2] in {(3,11),(3,12)} else 1)' >/dev/null 2>&1; then
    ok "Bot Python runtime is supported ($runtime_python)"
    python_ok=1
    # uv-managed virtualenvs do not need an installed pip module.
    if "$runtime_python" -I -c 'import aiogram, fastapi, uvicorn, psycopg' >/dev/null 2>&1; then
      ok "Controller Python dependencies are importable"
      dependencies_ok=1
    else
      fail "Controller Python dependencies are missing or cannot be imported"
      add_remediation "Repair the installed Python environment through installation diagnostics or rerun the supported install/update workflow"
    fi
  else
    fail "No supported installed bot Python runtime found at $runtime_python"
    add_remediation "Repair the managed virtualenv through installation diagnostics; no system Python replacement is required"
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
    add_remediation "Check the installed environment file at ${shared_dir}/.env and its permissions"
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
  runtime_env_file="$ENV_FILE"
  db_backend="$(read_env_value DB_BACKEND "$runtime_env_file")"
  postgres_dsn="$(read_env_value POSTGRES_DSN "$runtime_env_file")"
  if [[ -z "$db_backend" ]]; then
    db_backend="postgres"
  fi

  if [[ "$db_backend" == "postgres" && -n "$postgres_dsn" ]]; then
    ok "PostgreSQL runtime is configured"
  elif [[ "$db_backend" == "postgres" ]]; then
    fail "POSTGRES_DSN is missing for PostgreSQL runtime"
    add_remediation "Set POSTGRES_DSN in ${shared_dir}/.env and rerun installation/update"
  else
    warn "No PostgreSQL configuration was detected"
    add_remediation "Set POSTGRES_DSN in ${shared_dir}/.env and apply database migrations: PYTHONPATH=app .venv/bin/python -m backend.admin_cli init-schema"
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
      if systemctl is-active --quiet node-plane-backend-worker.service; then
        ok "node-plane-backend-worker.service is active"
      else
        warn "node-plane-backend-worker.service is not active"
        add_remediation "Start the worker service: sudo systemctl enable --now node-plane-backend-worker.service"
      fi
      if [[ -x "${app_dir}/.venv/bin/python" ]] && "${app_dir}/.venv/bin/python" -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8080/health/ready", timeout=3).read()' >/dev/null 2>&1; then
        ok "backend API is ready on loopback"
      else
        warn "backend API readiness check failed"
        add_remediation "Inspect sudo journalctl -u node-plane-backend and verify backend database migrations"
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
      info "Local node prerequisite: Docker daemon is not reachable for the current user"
      info "Docker access is needed for a local VPN node or managed local PostgreSQL"
    fi
  else
    info "Local node prerequisite: Docker is not installed"
    info "Docker is needed for a local VPN node or managed local PostgreSQL"
  fi

  if [[ -c /dev/net/tun ]]; then
    ok "/dev/net/tun is available"
    tun_ok=1
  else
    info "Local node prerequisite: /dev/net/tun is missing (not required for a controller-only host)"
    info "A local VPN node needs kernel TUN support; remote nodes are unaffected"
  fi

  if [[ $python_ok -eq 1 && $dependencies_ok -eq 1 && $venv_ok -eq 1 && $systemd_ok -eq 1 && $service_active_ok -eq 1 && $docker_ok -eq 1 && $tun_ok -eq 1 && $shared_data_ok -eq 1 && $shared_ssh_ok -eq 1 && $current_link_ok -eq 1 ]]; then
    SIMPLE_LOCAL_READY=1
  fi
}

print_mode_readiness() {
  section "Readiness"
  if [[ $SIMPLE_LOCAL_READY -eq 1 ]]; then
    ok "This host is ready for Simple Mode and local node deployment"
    info "Next: open the bot as admin, add a node, install its agent, then open Bootstrap"
  else
    info "Local node deployment prerequisites are incomplete; a controller can manage remote nodes without local VPN support"
    info "Goal: active systemd services, reachable Docker daemon, and /dev/net/tun on the same host"
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
  simple) ;;
  *)
    echo "Unsupported mode: ${MODE}. Use auto or simple." >&2
    exit 1
    ;;
esac
resolve_layout

check_repo_basics
check_env

check_simple_mode

print_mode_readiness
print_summary
print_remediations

if [[ $FAILURES -gt 0 ]]; then
  exit 1
fi
