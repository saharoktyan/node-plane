#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

MODE="${MODE:-}"
NON_INTERACTIVE=0
AUTO_INSTALL_SYSTEMD=0

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
    --install-systemd)
      AUTO_INSTALL_SYSTEMD=1
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage:
  scripts/install.sh [--mode simple|portable] [--non-interactive] [--install-systemd]

Modes:
  simple    Host install via venv + systemd. Supports same-host runtime deployment.
  portable  Docker-based bot install. Intended for remote SSH-managed nodes.

Flags:
  --non-interactive   Fail instead of prompting for missing values
  --install-systemd   In simple mode, install the systemd unit automatically
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

ensure_common_dirs() {
  mkdir -p data ssh scripts
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
  if command -v git >/dev/null 2>&1 && git rev-parse --short HEAD >/dev/null 2>&1; then
    git rev-parse --short HEAD
  else
    echo "unknown"
  fi
}

current_semver() {
  if [[ -f "${REPO_ROOT}/VERSION" ]]; then
    tr -d '\n' < "${REPO_ROOT}/VERSION"
  else
    echo "0.1.0"
  fi
}

release_id() {
  local semver commit
  semver="$(current_semver)"
  commit="$(current_git_commit)"
  if [[ "$commit" == "unknown" ]]; then
    echo "${semver}-$(date +%Y%m%d%H%M%S)"
  else
    echo "${semver}-${commit}"
  fi
}

export_release_tree() {
  local destination="$1"
  mkdir -p "$destination"
  if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git archive HEAD | tar -xf - -C "$destination"
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
  printf '%s\n' "$(current_git_commit)" > "${destination}/BUILD_COMMIT"
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

  local bot_token admin_ids base_dir app_dir shared_dir source_dir install_mode ssh_key image_repo image_tag
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
      image_tag="$(read_version)"
    fi
    if [[ $NON_INTERACTIVE -eq 0 ]]; then
      image_repo="$(prompt_value "Enter NODE_PLANE_IMAGE_REPO (default: ghcr.io/seventh7dev/node-plane, or use node-plane for local builds)" "$image_repo")"
      image_tag="$(prompt_value "Enter NODE_PLANE_IMAGE_TAG (use local for local builds)" "$image_tag")"
    fi
    set_env_value SSH_KEY "$ssh_key"
    set_env_value NODE_PLANE_SOURCE_DIR "$REPO_ROOT"
    set_env_value NODE_PLANE_INSTALL_MODE "$install_mode"
    set_env_value NODE_PLANE_IMAGE_REPO "$image_repo"
    set_env_value NODE_PLANE_IMAGE_TAG "$image_tag"
  fi

  set_env_value BOT_TOKEN "$bot_token"
  set_env_value ADMIN_IDS "$admin_ids"
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

run_simple_install() {
  local service_name="node-plane"
  local base_dir app_dir shared_dir releases_dir current_link new_release_dir release_name
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
  release_name="$(release_id)"
  new_release_dir="${releases_dir}/${release_name}"

  need_cmd python3
  ensure_supported_python

  mkdir -p "${releases_dir}" "${shared_dir}/data" "${shared_dir}/ssh"
  sync_shared_env "$shared_dir"
  rm -rf "$new_release_dir"
  export_release_tree "$new_release_dir"

  python3 -m venv "${new_release_dir}/.venv"
  "${new_release_dir}/.venv/bin/python" -m pip install --upgrade pip setuptools wheel
  "${new_release_dir}/.venv/bin/python" -m pip install -r "${new_release_dir}/requirements.txt"
  NODE_PLANE_BASE_DIR="${base_dir}" \
  NODE_PLANE_APP_DIR="${new_release_dir}" \
  NODE_PLANE_SHARED_DIR="${shared_dir}" \
  "${new_release_dir}/.venv/bin/python" "${new_release_dir}/app/manage_db.py" init

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
  need_cmd docker
  local image_repo image_tag
  image_repo="$(read_env_value NODE_PLANE_IMAGE_REPO)"
  image_tag="$(read_env_value NODE_PLANE_IMAGE_TAG)"
  if docker compose version >/dev/null 2>&1; then
    if [[ "${image_repo:-node-plane}" == "node-plane" && "${image_tag:-local}" == "local" ]]; then
      docker compose up -d --build
    else
      docker compose pull
      docker compose up -d
    fi
  elif command -v docker-compose >/dev/null 2>&1; then
    if [[ "${image_repo:-node-plane}" == "node-plane" && "${image_tag:-local}" == "local" ]]; then
      docker-compose up -d --build
    else
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
