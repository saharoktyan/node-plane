#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

APP_ROOT="${NODE_PLANE_APP_DIR:-$REPO_ROOT}"
BASE_ROOT="${NODE_PLANE_BASE_DIR:-}"
SHARED_ROOT="${NODE_PLANE_SHARED_DIR:-}"
if [[ -z "$SHARED_ROOT" ]]; then
  if [[ -n "$BASE_ROOT" ]]; then
    SHARED_ROOT="${BASE_ROOT}/shared"
  elif [[ "$APP_ROOT" == */current ]]; then
    SHARED_ROOT="${APP_ROOT%/current}/shared"
  else
    SHARED_ROOT="${APP_ROOT}/shared"
  fi
fi
ENV_FILE="${SHARED_ROOT}/.env"
if [[ ! -f "$ENV_FILE" && -f "${APP_ROOT}/.env" ]]; then
  ENV_FILE="${APP_ROOT}/.env"
fi
if [[ ! -f "$ENV_FILE" && -f "${REPO_ROOT}/.env" ]]; then
  ENV_FILE="${REPO_ROOT}/.env"
fi

SKIP_DRIVER=0
SKIP_AGENTS=0
STRICT_MODE=0
DRY_RUN=0
AGENT_PORT="${NODE_AGENT_PORT:-50061}"
BIN_SOURCE="${NODE_PLANE_BIN_SOURCE:-auto}" # auto|release|build
GITHUB_REPO="${NODE_PLANE_GITHUB_REPO:-seventh7dev/node-plane}"
RELEASE_REF="${NODE_PLANE_BINARY_RELEASE:-}"
DRIVER_ASSET_NAME="${NODE_PLANE_DRIVER_ASSET_NAME:-node-plane-driver-linux-amd64.tar.gz}"
AGENT_ASSET_NAME="${NODE_PLANE_AGENT_ASSET_NAME:-node-plane-agent-linux-amd64.tar.gz}"
DRIVER_BIN_URL="${NODE_PLANE_DRIVER_BIN_URL:-}"
AGENT_BIN_URL="${NODE_PLANE_AGENT_BIN_URL:-}"
CURRENT_STEP="startup"
DRIVER_BIN_CHANGED=0
DRIVER_UNIT_CHANGED=0
ENV_CHANGED=0

set_step() {
  CURRENT_STEP="$1"
}

on_error() {
  local exit_code="$1"
  echo >&2
  echo "Driver/agent setup failed during step: ${CURRENT_STEP}" >&2
  echo "Failing command: ${BASH_COMMAND}" >&2
  echo "Exit code: ${exit_code}" >&2
}

trap 'on_error $?' ERR

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
  local file="$2"
  if [[ ! -f "$file" ]]; then
    return 0
  fi
  sed -n "s/^${key}=//p" "$file" | tail -n 1
}

set_env_value() {
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

set_env_value_if_changed() {
  local file="$1"
  local key="$2"
  local value="$3"
  local current
  current="$(read_env_value "$key" "$file")"
  if [[ "$current" == "$value" ]]; then
    return 1
  fi
  set_env_value "$file" "$key" "$value"
  return 0
}

sha256_of_file() {
  local path="$1"
  sha256sum "$path" | awk '{print $1}'
}

download_to_file() {
  local url="$1"
  local out="$2"
  if has_cmd curl; then
    curl -fsSL "$url" -o "$out"
    return 0
  fi
  if has_cmd wget; then
    wget -qO "$out" "$url"
    return 0
  fi
  echo "Neither curl nor wget is available for downloading binaries." >&2
  return 1
}

check_url_access() {
  local url="$1"
  if has_cmd curl; then
    curl -fsSLI "$url" >/dev/null
    return 0
  fi
  if has_cmd wget; then
    wget -q --spider "$url"
    return 0
  fi
  echo "Neither curl nor wget is available for URL checks." >&2
  return 1
}

detect_release_ref() {
  if [[ -n "$RELEASE_REF" ]]; then
    echo "$RELEASE_REF"
    return 0
  fi
  if [[ -f "${APP_ROOT}/VERSION" ]]; then
    local semver
    semver="$(tr -d '\n' < "${APP_ROOT}/VERSION")"
    if [[ -n "$semver" ]]; then
      echo "v${semver}"
      return 0
    fi
  fi
  echo "latest"
}

asset_url() {
  local asset_name="$1"
  local ref
  ref="$(detect_release_ref)"
  if [[ "$ref" == "latest" ]]; then
    echo "https://github.com/${GITHUB_REPO}/releases/latest/download/${asset_name}"
  else
    echo "https://github.com/${GITHUB_REPO}/releases/download/${ref}/${asset_name}"
  fi
}

ensure_bin_source_mode() {
  case "$BIN_SOURCE" in
    auto|release|build) ;;
    *)
      echo "Unsupported NODE_PLANE_BIN_SOURCE value: $BIN_SOURCE" >&2
      exit 1
      ;;
  esac
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-driver)
      SKIP_DRIVER=1
      shift
      ;;
    --skip-agents)
      SKIP_AGENTS=1
      shift
      ;;
    --strict)
      STRICT_MODE=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --agent-port)
      AGENT_PORT="${2:-}"
      shift 2
      ;;
    --agent-port=*)
      AGENT_PORT="${1#*=}"
      shift
      ;;
    --bin-source)
      BIN_SOURCE="${2:-}"
      shift 2
      ;;
    --bin-source=*)
      BIN_SOURCE="${1#*=}"
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage:
  scripts/setup_driver_agents.sh [--skip-driver] [--skip-agents] [--agent-port 50061] [--strict] [--dry-run] [--bin-source auto|release|build]

Purpose:
  - Install local node-plane-driver as a systemd service.
  - Deploy node-plane-agent binary to SSH-managed nodes from the server registry.
  - Write NODE_AGENT_TARGETS and switch bot to grpc driver backend in the shared .env.

Binary source modes:
  auto     Try GitHub release binaries first; fallback to local cargo build.
  release  Use GitHub release binaries only.
  build    Use local cargo build only.

Dry run:
  --dry-run validates binary source resolution and SSH reachability only.
  It does not install binaries, write systemd units, or restart services.

Key env overrides:
  NODE_PLANE_BIN_SOURCE             auto|release|build (default: auto)
  NODE_PLANE_GITHUB_REPO            owner/repo (default: seventh7dev/node-plane)
  NODE_PLANE_BINARY_RELEASE         release tag (default: v<VERSION> from app root)
  NODE_PLANE_DRIVER_ASSET_NAME      driver asset filename
  NODE_PLANE_AGENT_ASSET_NAME       agent asset filename
  NODE_PLANE_DRIVER_BIN_URL         explicit driver binary URL
  NODE_PLANE_AGENT_BIN_URL          explicit agent binary URL
EOF
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

ensure_bin_source_mode

if [[ ! "$AGENT_PORT" =~ ^[0-9]+$ ]]; then
  echo "Invalid --agent-port: $AGENT_PORT" >&2
  exit 1
fi

need_cmd python3
need_cmd ssh
need_cmd scp
need_cmd sudo

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

APP_ROOT="$(cd "$APP_ROOT" && pwd)"
if [[ ! -d "${APP_ROOT}/rust/node-driver" || ! -d "${APP_ROOT}/rust/node-agent" ]]; then
  echo "Rust driver/agent sources are not present under APP_ROOT=${APP_ROOT}" >&2
  exit 1
fi

PYTHON_BIN="python3"
if [[ -x "${APP_ROOT}/.venv/bin/python" ]]; then
  PYTHON_BIN="${APP_ROOT}/.venv/bin/python"
fi

echo "Using APP_ROOT=${APP_ROOT}"
echo "Using ENV_FILE=${ENV_FILE}"
echo "Binary source mode: ${BIN_SOURCE}"

WORK_DIR="$(mktemp -d)"
cleanup_workdir() {
  rm -rf "$WORK_DIR"
}
trap cleanup_workdir EXIT

driver_bin_path=""
agent_bin_path=""

download_release_binaries() {
  local driver_url="${DRIVER_BIN_URL:-$(asset_url "$DRIVER_ASSET_NAME")}"
  local agent_url="${AGENT_BIN_URL:-$(asset_url "$AGENT_ASSET_NAME")}"
  local driver_out="${WORK_DIR}/node-plane-driver"
  local agent_out="${WORK_DIR}/node-plane-agent"
  local driver_archive="${WORK_DIR}/driver.asset"
  local agent_archive="${WORK_DIR}/agent.asset"

  set_step "download driver binary"
  download_to_file "$driver_url" "$driver_archive" || return 1
  if tar -tzf "$driver_archive" >/dev/null 2>&1; then
    tar -xzf "$driver_archive" -C "$WORK_DIR" || return 1
    if [[ -x "${WORK_DIR}/node-plane-driver-linux-amd64" ]]; then
      mv "${WORK_DIR}/node-plane-driver-linux-amd64" "$driver_out"
    elif [[ -x "${WORK_DIR}/node-plane-driver" ]]; then
      mv "${WORK_DIR}/node-plane-driver" "$driver_out"
    else
      return 1
    fi
  else
    mv "$driver_archive" "$driver_out"
  fi
  chmod +x "$driver_out" || return 1

  set_step "download agent binary"
  download_to_file "$agent_url" "$agent_archive" || return 1
  if tar -tzf "$agent_archive" >/dev/null 2>&1; then
    tar -xzf "$agent_archive" -C "$WORK_DIR" || return 1
    if [[ -x "${WORK_DIR}/node-plane-agent-linux-amd64" ]]; then
      mv "${WORK_DIR}/node-plane-agent-linux-amd64" "$agent_out"
    elif [[ -x "${WORK_DIR}/node-plane-agent" ]]; then
      mv "${WORK_DIR}/node-plane-agent" "$agent_out"
    else
      return 1
    fi
  else
    mv "$agent_archive" "$agent_out"
  fi
  chmod +x "$agent_out" || return 1

  driver_bin_path="$driver_out"
  agent_bin_path="$agent_out"
}

build_local_binaries() {
  need_cmd cargo
  set_step "build node-driver binary"
  (cd "${APP_ROOT}/rust/node-driver" && cargo build --release)
  set_step "build node-agent binary"
  (cd "${APP_ROOT}/rust/node-agent" && cargo build --release)
  driver_bin_path="${APP_ROOT}/rust/node-driver/target/release/node-plane-driver"
  agent_bin_path="${APP_ROOT}/rust/node-agent/target/release/node-plane-agent"
}

resolve_binaries() {
  case "$BIN_SOURCE" in
    release)
      download_release_binaries
      ;;
    build)
      build_local_binaries
      ;;
    auto)
      if download_release_binaries; then
        echo "Using release binaries from GitHub."
      else
        echo "Release download failed; falling back to local cargo build."
        build_local_binaries
      fi
      ;;
  esac
  [[ -x "$driver_bin_path" ]] || { echo "Driver binary is not executable: $driver_bin_path" >&2; exit 1; }
  [[ -x "$agent_bin_path" ]] || { echo "Agent binary is not executable: $agent_bin_path" >&2; exit 1; }
}

resolve_binaries_dry_run() {
  local driver_url="${DRIVER_BIN_URL:-$(asset_url "$DRIVER_ASSET_NAME")}"
  local agent_url="${AGENT_BIN_URL:-$(asset_url "$AGENT_ASSET_NAME")}"
  case "$BIN_SOURCE" in
    release)
      set_step "dry-run check release binary urls"
      check_url_access "$driver_url"
      check_url_access "$agent_url"
      echo "Dry-run: release binary URLs are reachable."
      ;;
    build)
      need_cmd cargo
      echo "Dry-run: local cargo build mode is available."
      ;;
    auto)
      set_step "dry-run check release binary urls"
      if check_url_access "$driver_url" && check_url_access "$agent_url"; then
        echo "Dry-run: release binary URLs are reachable (auto mode)."
      else
        echo "Dry-run: release binary URLs are not reachable, auto mode would fallback to local build."
        need_cmd cargo
      fi
      ;;
  esac
}

install_local_driver() {
  local new_sum current_sum unit_target
  new_sum="$(sha256_of_file "$driver_bin_path")"
  current_sum=""
  unit_target="/etc/systemd/system/node-plane-driver.service"
  if sudo test -x /usr/local/bin/node-plane-driver; then
    current_sum="$(sudo sha256sum /usr/local/bin/node-plane-driver | awk '{print $1}')"
  fi
  if [[ "$new_sum" != "$current_sum" ]]; then
    set_step "install local node-plane-driver binary"
    sudo install -m 0755 "$driver_bin_path" /usr/local/bin/node-plane-driver
    DRIVER_BIN_CHANGED=1
    echo "Updated node-plane-driver binary."
  else
    echo "node-plane-driver binary is up to date; skipping reinstall."
  fi

  local unit_tmp
  unit_tmp="$(mktemp)"
  cat > "$unit_tmp" <<EOF
[Unit]
Description=Node Plane Driver
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_ROOT}
Environment=NODE_PLANE_BASE_DIR=${NODE_PLANE_BASE_DIR:-/opt/node-plane}
Environment=NODE_PLANE_APP_DIR=${APP_ROOT}
Environment=NODE_PLANE_SHARED_DIR=${NODE_PLANE_SHARED_DIR:-${SHARED_ROOT}}
EnvironmentFile=${ENV_FILE}
ExecStart=/usr/local/bin/node-plane-driver
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

  set_step "install node-plane-driver systemd unit"
  if sudo test -f "$unit_target" && sudo cmp -s "$unit_tmp" "$unit_target"; then
    echo "node-plane-driver.service is up to date."
  else
    sudo install -m 0644 "$unit_tmp" "$unit_target"
    DRIVER_UNIT_CHANGED=1
    echo "Updated node-plane-driver.service unit."
  fi
  rm -f "$unit_tmp"
  if [[ $DRIVER_UNIT_CHANGED -eq 1 ]]; then
    sudo systemctl daemon-reload
  fi
  sudo systemctl enable --now node-plane-driver
  if [[ $DRIVER_BIN_CHANGED -eq 1 || $DRIVER_UNIT_CHANGED -eq 1 ]]; then
    sudo systemctl restart node-plane-driver
  fi
  sudo systemctl status node-plane-driver --no-pager || true
}

list_remote_servers_tsv() {
  PYTHONPATH="${APP_ROOT}/app" NODE_PLANE_APP_DIR="${APP_ROOT}" NODE_PLANE_SHARED_DIR="${SHARED_ROOT}" "$PYTHON_BIN" - <<'PY'
from services.server_registry import list_servers

for srv in list_servers(include_disabled=False):
    if srv.transport == "local":
        continue
    if not srv.ssh_target:
        continue
    ssh_host = (srv.ssh_host or "").strip()
    ssh_user = (srv.ssh_user or "").strip()
    ssh_key = (srv.ssh_key_path or "").strip()
    public_host = (srv.public_host or ssh_host or "").strip()
    if not ssh_host:
        continue
    print("\t".join([
        srv.key,
        ssh_host,
        str(srv.ssh_port or 22),
        ssh_user,
        ssh_key,
        public_host,
    ]))
PY
}

deploy_agents() {
  local lines
  if ! lines="$(list_remote_servers_tsv)"; then
    echo "Failed to query server registry from APP_ROOT=${APP_ROOT} using ${PYTHON_BIN}" >&2
    if [[ $STRICT_MODE -eq 1 ]]; then
      return 1
    fi
    echo "Skipping node-agent deploy (best-effort mode)." >&2
    return 0
  fi
  if [[ -z "$lines" ]]; then
    echo "No SSH-managed enabled servers found; skipping node-agent deploy."
    return 0
  fi

  local failed=0
  local mappings=()
  local local_agent_sum
  local_agent_sum="$(sha256_of_file "$agent_bin_path")"
  while IFS=$'\t' read -r server_key ssh_host ssh_port ssh_user ssh_key public_host; do
    [[ -z "$server_key" ]] && continue
    local target_host="$ssh_host"
    local target="${ssh_user:+${ssh_user}@}${target_host}"
    local reach_host="${public_host:-$ssh_host}"
    mappings+=("${server_key}=${reach_host}:${AGENT_PORT}")

    echo
    echo "Deploying node-agent to ${server_key} (${target}:${ssh_port})..."

    local -a ssh_opts=("-p" "$ssh_port" "-o" "BatchMode=yes" "-o" "StrictHostKeyChecking=${SSH_STRICT_HOST_KEY_CHECKING:-accept-new}")
    if [[ -n "${SSH_KNOWN_HOSTS_PATH:-}" ]]; then
      ssh_opts+=("-o" "UserKnownHostsFile=${SSH_KNOWN_HOSTS_PATH}")
    fi
    if [[ -n "$ssh_key" ]]; then
      ssh_opts+=("-i" "$ssh_key")
    elif [[ -n "${SSH_KEY:-}" ]]; then
      ssh_opts+=("-i" "${SSH_KEY}")
    fi

    if [[ $DRY_RUN -eq 1 ]]; then
      if ! ssh "${ssh_opts[@]}" "$target" 'echo "node-plane-agent dry-run ok" >/dev/null'; then
        echo "Dry-run SSH check failed for ${server_key}" >&2
        failed=$((failed + 1))
      else
        echo "Dry-run SSH check passed for ${server_key}"
      fi
      continue
    fi

    local remote_sum=""
    remote_sum="$(ssh "${ssh_opts[@]}" "$target" 'if [ -x /usr/local/bin/node-plane-agent ]; then sha256sum /usr/local/bin/node-plane-agent | awk "{print \$1}"; fi' 2>/dev/null || true)"
    if [[ "$remote_sum" == "$local_agent_sum" ]]; then
      if ssh "${ssh_opts[@]}" "$target" 'sudo systemctl is-active --quiet node-plane-agent' >/dev/null 2>&1; then
        echo "node-agent is up to date and active on ${server_key}; skipping reinstall."
        continue
      fi
      if ssh "${ssh_opts[@]}" "$target" 'sudo systemctl restart node-plane-agent && sudo systemctl is-active --quiet node-plane-agent' >/dev/null 2>&1; then
        echo "node-agent binary unchanged; service restarted on ${server_key}."
        continue
      fi
      echo "node-agent service restart failed on ${server_key}" >&2
      failed=$((failed + 1))
      continue
    fi

    if ! scp "${ssh_opts[@]}" "$agent_bin_path" "${target}:/tmp/node-plane-agent"; then
      echo "Failed to copy agent binary to ${server_key}" >&2
      failed=$((failed + 1))
      continue
    fi

    local remote_script
    remote_script="$(mktemp)"
    cat > "$remote_script" <<EOF
set -euo pipefail
sudo install -m 0755 /tmp/node-plane-agent /usr/local/bin/node-plane-agent
sudo rm -f /tmp/node-plane-agent
sudo mkdir -p /etc/node-plane
sudo tee /etc/node-plane/agent.toml >/dev/null <<'AGENTCFG'
node_key = "${server_key}"
listen_addr = "0.0.0.0:${AGENT_PORT}"
AGENTCFG
sudo tee /etc/systemd/system/node-plane-agent.service >/dev/null <<'UNIT'
[Unit]
Description=Node Plane Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=NODE_AGENT_CONFIG_PATH=/etc/node-plane/agent.toml
Environment=NODE_AGENT_NODE_KEY=${server_key}
Environment=NODE_AGENT_LISTEN_ADDR=0.0.0.0:${AGENT_PORT}
ExecStart=/usr/local/bin/node-plane-agent
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now node-plane-agent
sudo systemctl restart node-plane-agent
sudo systemctl is-active --quiet node-plane-agent
EOF
    if ! ssh "${ssh_opts[@]}" "$target" 'bash -s' < "$remote_script"; then
      echo "Failed to install/start node-agent on ${server_key}" >&2
      failed=$((failed + 1))
      rm -f "$remote_script"
      continue
    fi
    rm -f "$remote_script"
    echo "node-agent is active on ${server_key}"
  done <<< "$lines"

  if [[ ${#mappings[@]} -gt 0 ]]; then
    local mapping_csv
    mapping_csv="$(IFS=,; echo "${mappings[*]}")"
    if [[ $DRY_RUN -eq 1 ]]; then
      echo "Dry-run mapping preview: ${mapping_csv}"
    else
      set_step "write driver/agent env configuration"
      if set_env_value_if_changed "$ENV_FILE" "NODE_DRIVER_BACKEND" "grpc"; then ENV_CHANGED=1; fi
      if set_env_value_if_changed "$ENV_FILE" "NODE_DRIVER_GRPC_TARGET" "127.0.0.1:50051"; then ENV_CHANGED=1; fi
      if set_env_value_if_changed "$ENV_FILE" "NODE_AGENT_TARGETS" "$mapping_csv"; then ENV_CHANGED=1; fi
      echo "Configured NODE_AGENT_TARGETS in ${ENV_FILE}: ${mapping_csv}"
    fi
  fi

  if [[ $failed -gt 0 ]]; then
    if [[ $STRICT_MODE -eq 1 ]]; then
      echo "node-agent deploy failures: ${failed}" >&2
      return 1
    fi
    echo "node-agent deploy completed with ${failed} failures (best-effort mode)."
  fi
}

if [[ $DRY_RUN -eq 1 ]]; then
  resolve_binaries_dry_run
else
  resolve_binaries
fi

if [[ $SKIP_DRIVER -eq 0 && $DRY_RUN -eq 0 ]]; then
  install_local_driver
fi
if [[ $SKIP_AGENTS -eq 0 ]]; then
  deploy_agents
fi

if [[ $SKIP_DRIVER -eq 0 && $DRY_RUN -eq 0 ]]; then
  set_step "restart node-plane-driver with updated env"
  if [[ $DRIVER_BIN_CHANGED -eq 1 || $DRIVER_UNIT_CHANGED -eq 1 || $ENV_CHANGED -eq 1 ]]; then
    sudo systemctl restart node-plane-driver || true
  else
    echo "No local driver/env changes detected; skipping final node-plane-driver restart."
  fi
fi

echo
if [[ $DRY_RUN -eq 1 ]]; then
  echo "Driver/agent dry-run finished."
else
  echo "Driver/agent setup finished."
fi
