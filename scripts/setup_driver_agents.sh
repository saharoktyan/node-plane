#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

checkout_env_value() {
  local key="$1"
  if [[ -f "${REPO_ROOT}/.env" ]]; then
    sed -n "s/^${key}=//p" "${REPO_ROOT}/.env" | tail -n 1
  fi
}

APP_ROOT="${NODE_PLANE_APP_DIR:-}"
if [[ -z "$APP_ROOT" ]]; then
  APP_ROOT="$(checkout_env_value NODE_PLANE_APP_DIR)"
  if [[ -z "$APP_ROOT" || ! -d "$APP_ROOT" ]]; then
    APP_ROOT="$REPO_ROOT"
  fi
fi
BASE_ROOT="${NODE_PLANE_BASE_DIR:-$(checkout_env_value NODE_PLANE_BASE_DIR)}"
SHARED_ROOT="${NODE_PLANE_SHARED_DIR:-$(checkout_env_value NODE_PLANE_SHARED_DIR)}"
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
ONLY_NODE_KEY=""
BIN_SOURCE="${NODE_PLANE_BIN_SOURCE:-auto}" # auto|release|build
GITHUB_REPO="${NODE_PLANE_GITHUB_REPO:-saharoktyan/node-plane}"
TLS_ROOT="${SHARED_ROOT}/driver-agent-tls"
TLS_CA_CERT="${TLS_ROOT}/ca.crt"
TLS_CA_KEY="${TLS_ROOT}/ca.key"
TLS_CLIENT_CERT="${TLS_ROOT}/driver-client.crt"
TLS_CLIENT_KEY="${TLS_ROOT}/driver-client.key"
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

show_agent_service_diagnostics() {
  local target="$1"
  shift
  echo "node-plane-agent systemd status on ${target}:" >&2
  ssh "$@" "$target" 'sudo systemctl status node-plane-agent --no-pager -l || true; echo "Recent node-plane-agent journal:"; sudo journalctl -u node-plane-agent -n 30 --no-pager || true' >&2 || true
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

certificate_matches_key() {
  local certificate="$1" private_key="$2" cert_public key_public
  cert_public="$(openssl x509 -in "$certificate" -pubkey -noout \
    | openssl pkey -pubin -outform DER 2>/dev/null | sha256sum | awk '{print $1}')" || return 1
  key_public="$(openssl pkey -in "$private_key" -pubout -outform DER \
    2>/dev/null | sha256sum | awk '{print $1}')" || return 1
  [[ -n "$cert_public" && "$cert_public" == "$key_public" ]]
}

issue_driver_client_certificate() {
  local client_tmp
  client_tmp="$(mktemp -d "${TLS_ROOT}/.client.XXXXXX")"
  openssl req -new -newkey rsa:2048 -nodes -sha256 \
    -keyout "${client_tmp}/driver-client.key" \
    -out "${client_tmp}/driver-client.csr" -subj "/CN=node-plane-driver"
  cat > "${client_tmp}/client.ext" <<'EXT'
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=clientAuth
EXT
  openssl x509 -req -in "${client_tmp}/driver-client.csr" \
    -CA "$TLS_CA_CERT" -CAkey "$TLS_CA_KEY" -CAcreateserial \
    -days 825 -sha256 -extfile "${client_tmp}/client.ext" \
    -out "${client_tmp}/driver-client.crt"
  install -m 0600 "${client_tmp}/driver-client.key" "$TLS_CLIENT_KEY"
  install -m 0644 "${client_tmp}/driver-client.crt" "$TLS_CLIENT_CERT"
  rm -rf "$client_tmp"
}

prepare_driver_agent_tls() {
  need_cmd openssl
  umask 077
  mkdir -p "$TLS_ROOT/nodes"
  chmod 0700 "$TLS_ROOT" "$TLS_ROOT/nodes"
  if [[ ! -e "$TLS_CA_CERT" && ! -e "$TLS_CA_KEY" ]]; then
    local ca_tmp
    ca_tmp="$(mktemp -d "${TLS_ROOT}/.ca.XXXXXX")"
    openssl req -x509 -newkey rsa:3072 -nodes -sha256 -days 3650 \
      -keyout "${ca_tmp}/ca.key" -out "${ca_tmp}/ca.crt" \
      -subj "/CN=Node Plane Driver Agent CA" \
      -addext "basicConstraints=critical,CA:TRUE" \
      -addext "keyUsage=critical,keyCertSign,cRLSign"
    install -m 0600 "${ca_tmp}/ca.key" "$TLS_CA_KEY"
    install -m 0644 "${ca_tmp}/ca.crt" "$TLS_CA_CERT"
    rm -rf "$ca_tmp"
  elif [[ ! -s "$TLS_CA_CERT" || ! -s "$TLS_CA_KEY" ]]; then
    echo "Incomplete driver-agent CA material in ${TLS_ROOT}; refusing to replace it." >&2
    return 1
  fi
  chmod 0600 "$TLS_CA_KEY"
  chmod 0644 "$TLS_CA_CERT"
  if ! certificate_matches_key "$TLS_CA_CERT" "$TLS_CA_KEY"; then
    echo "Driver-agent CA certificate and private key do not match." >&2
    return 1
  fi
  if ! openssl x509 -checkend 15552000 -noout -in "$TLS_CA_CERT" >/dev/null 2>&1; then
    echo "Driver-agent CA expires within 180 days; rotate the CA and reissue certificates before rollout." >&2
    return 1
  fi

  if [[ ! -e "$TLS_CLIENT_CERT" && ! -e "$TLS_CLIENT_KEY" ]]; then
    issue_driver_client_certificate
  elif [[ ! -s "$TLS_CLIENT_CERT" || ! -s "$TLS_CLIENT_KEY" ]]; then
    echo "Incomplete driver client certificate in ${TLS_ROOT}; refusing to replace it." >&2
    return 1
  elif ! openssl x509 -checkend 2592000 -noout -in "$TLS_CLIENT_CERT" >/dev/null 2>&1 \
    || ! openssl verify -CAfile "$TLS_CA_CERT" "$TLS_CLIENT_CERT" >/dev/null 2>&1 \
    || ! certificate_matches_key "$TLS_CLIENT_CERT" "$TLS_CLIENT_KEY"; then
    issue_driver_client_certificate
  fi
  chmod 0600 "$TLS_CLIENT_KEY"
  chmod 0644 "$TLS_CLIENT_CERT"
}

prepare_node_tls() {
  local server_key="$1"
  local server_host="$2"
  local san_host="$server_host"
  if [[ ! "$server_key" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
    echo "Unsafe node key for TLS rollout: ${server_key}" >&2
    return 1
  fi
  if [[ "$san_host" == \[*\] ]]; then
    san_host="${san_host:1:${#san_host}-2}"
  fi
  if [[ "$san_host" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ || "$san_host" == *:* ]]; then
    NODE_TLS_SAN="IP:${san_host}"
  elif [[ "$san_host" =~ ^[A-Za-z0-9.-]+$ ]]; then
    NODE_TLS_SAN="DNS:${san_host}"
  else
    echo "Node agent TLS requires an IPv4 address or DNS host, got: ${server_host}" >&2
    return 1
  fi
  NODE_TLS_DIR="${TLS_ROOT}/nodes/${server_key}"
  NODE_TLS_CERT="${NODE_TLS_DIR}/server.crt"
  NODE_TLS_KEY="${NODE_TLS_DIR}/server.key"
  mkdir -p "$NODE_TLS_DIR"
  chmod 0700 "$NODE_TLS_DIR"

  local expected_san="${NODE_TLS_DIR}/server.san"
  local regenerate=0
  if [[ ! -s "$NODE_TLS_CERT" || ! -s "$NODE_TLS_KEY" || ! -s "$expected_san" ]] \
    || [[ "$(cat "$expected_san" 2>/dev/null || true)" != "$NODE_TLS_SAN" ]] \
    || ! openssl x509 -checkend 2592000 -noout -in "$NODE_TLS_CERT" >/dev/null 2>&1 \
    || ! openssl verify -CAfile "$TLS_CA_CERT" "$NODE_TLS_CERT" >/dev/null 2>&1 \
    || ! certificate_matches_key "$NODE_TLS_CERT" "$NODE_TLS_KEY"; then
    regenerate=1
  fi
  if [[ $regenerate -eq 1 ]]; then
    local node_tmp
    node_tmp="$(mktemp -d "${NODE_TLS_DIR}/.cert.XXXXXX")"
    openssl req -new -newkey rsa:2048 -nodes -sha256 \
      -keyout "${node_tmp}/server.key" -out "${node_tmp}/server.csr" \
      -subj "/CN=${server_key}"
    cat > "${node_tmp}/server.ext" <<EXT
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=${NODE_TLS_SAN}
EXT
    openssl x509 -req -in "${node_tmp}/server.csr" \
      -CA "$TLS_CA_CERT" -CAkey "$TLS_CA_KEY" -CAcreateserial \
      -days 825 -sha256 -extfile "${node_tmp}/server.ext" \
      -out "${node_tmp}/server.crt"
    install -m 0600 "${node_tmp}/server.key" "$NODE_TLS_KEY"
    install -m 0644 "${node_tmp}/server.crt" "$NODE_TLS_CERT"
    printf '%s\n' "$NODE_TLS_SAN" > "$expected_san"
    chmod 0600 "$expected_san"
    rm -rf "$node_tmp"
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
    if curl -fsSL "$url" -o "$out"; then
      return 0
    fi
    rm -f "$out"
    return 1
  fi
  if has_cmd wget; then
    if wget -qO "$out" "$url"; then
      return 0
    fi
    rm -f "$out"
    return 1
  fi
  echo "Neither curl nor wget is available for downloading binaries." >&2
  return 1
}

ensure_cargo_for_auto_build() {
  if has_cmd cargo && has_cmd protoc; then
    return 0
  fi
  local answer="${NODE_PLANE_INSTALL_RUST:-ask}"
  if [[ "$answer" == "ask" && -t 0 && -r /dev/tty ]]; then
    read -r -p "Release binaries are unavailable. Install Rust build tools (cargo, rustc, protoc) locally? [y/N] " answer </dev/tty
  fi
  case "${answer,,}" in
    y|yes|1|true) ;;
    *)
      echo "RUST_INSTALL_REQUIRED: release binaries and local Rust build tools are unavailable. Run again with NODE_PLANE_INSTALL_RUST=yes to install build tools, or publish release binaries." >&2
      return 1
      ;;
  esac
  echo "Installing Rust build tools for a local build."
  if has_cmd apt-get; then
    sudo env DEBIAN_FRONTEND=noninteractive apt-get update
    sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y cargo rustc protobuf-compiler build-essential
  elif has_cmd dnf; then
    sudo dnf install -y cargo rustc protobuf-compiler gcc make
  else
    echo "No supported package manager found. Install cargo, rustc and protoc, then rerun with NODE_PLANE_BIN_SOURCE=build." >&2
    return 1
  fi
  need_cmd cargo
  need_cmd protoc
}

check_url_access() {
  local url="$1"
  if has_cmd curl; then
    curl -fsSLI "$url" >/dev/null
    return $?
  fi
  if has_cmd wget; then
    wget -q --spider "$url"
    return $?
  fi
  echo "Neither curl nor wget is available for URL checks." >&2
  return 1
}

release_asset_api_url() {
  local asset_name="$1"
  local metadata="${WORK_DIR}/release.metadata.json"
  if [[ ! -s "$metadata" ]]; then
    download_to_file "https://api.github.com/repos/${GITHUB_REPO}/releases/tags/$(detect_release_ref)" "$metadata" || return 1
  fi
  "$PYTHON_BIN" - "$metadata" "$asset_name" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    release = json.load(source)
for asset in release.get("assets", []):
    if asset.get("name") == sys.argv[2] and asset.get("state") == "uploaded":
        print(asset["url"])
        break
else:
    raise SystemExit(1)
PY
}

download_release_asset() {
  local asset_name="$1" direct_url="$2" out="$3" custom_url="$4" api_url
  if download_to_file "$direct_url" "$out"; then
    return 0
  fi
  [[ -z "$custom_url" ]] || return 1
  api_url="$(release_asset_api_url "$asset_name")" || return 1
  echo "Direct download unavailable; trying GitHub API for ${asset_name}."
  if has_cmd curl; then
    curl -fsSL -H 'Accept: application/octet-stream' "$api_url" -o "$out" && return 0
  elif has_cmd wget; then
    wget -qO "$out" --header='Accept: application/octet-stream' "$api_url" && return 0
  fi
  rm -f "$out"
  return 1
}

check_release_asset_access() {
  local asset_name="$1" direct_url="$2" custom_url="$3"
  if check_url_access "$direct_url"; then
    return 0
  fi
  [[ -z "$custom_url" ]] || return 1
  release_asset_api_url "$asset_name" >/dev/null
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
    --node-key)
      ONLY_NODE_KEY="${2:-}"
      shift 2
      ;;
    --node-key=*)
      ONLY_NODE_KEY="${1#*=}"
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
  scripts/setup_driver_agents.sh [--skip-driver] [--skip-agents] [--node-key KEY] [--agent-port 50061] [--strict] [--dry-run] [--bin-source auto|release|build]

Purpose:
  - Install local node-plane-driver as a systemd service.
  - Deploy node-plane-agent binary to SSH-managed nodes from the server registry, or one node with --node-key.
  - Write NODE_AGENT_TARGETS and switch bot to grpc driver backend in the shared .env.

Binary source modes:
  auto     Use GitHub release binaries first; if unavailable, build locally when resources and Rust tools are available.
  release  Use GitHub release binaries only.
  build    Use local cargo build only.

Dry run:
  --dry-run validates binary source resolution and SSH reachability only.
  It does not install binaries, write systemd units, or restart services.

Key env overrides:
  NODE_PLANE_BIN_SOURCE             auto|release|build (default: auto)
  NODE_PLANE_GITHUB_REPO            owner/repo (default: saharoktyan/node-plane)
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
  if ! download_release_asset "$DRIVER_ASSET_NAME" "$driver_url" "$driver_archive" "$DRIVER_BIN_URL"; then
    echo "Driver release asset ${DRIVER_ASSET_NAME} for $(detect_release_ref) is missing or inaccessible. Publish both release assets or configure authenticated binary URLs." >&2
    return 1
  fi
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
  if ! download_release_asset "$AGENT_ASSET_NAME" "$agent_url" "$agent_archive" "$AGENT_BIN_URL"; then
    echo "Agent release asset ${AGENT_ASSET_NAME} for $(detect_release_ref) is missing or inaccessible. Publish both release assets or configure authenticated binary URLs." >&2
    return 1
  fi
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
  need_cmd protoc
  check_local_build_resources
  export CARGO_BUILD_JOBS="${NODE_PLANE_BUILD_JOBS:-1}"
  set_step "build node-driver binary"
  (cd "${APP_ROOT}/rust/node-driver" && cargo build --release)
  set_step "build node-agent binary"
  (cd "${APP_ROOT}/rust/node-agent" && cargo build --release)
  driver_bin_path="${APP_ROOT}/rust/node-driver/target/release/node-plane-driver"
  agent_bin_path="${APP_ROOT}/rust/node-agent/target/release/node-plane-agent"
}

check_local_build_resources() {
  local min_mem_mb max_cpu_percent mem_available_kb cpu_before idle_before cpu_after idle_after cpu_busy_percent
  min_mem_mb="${NODE_PLANE_BUILD_MIN_MEM_MB:-2048}"
  max_cpu_percent="${NODE_PLANE_BUILD_MAX_CPU_PERCENT:-65}"
  if [[ ! "$min_mem_mb" =~ ^[0-9]+$ || ! "$max_cpu_percent" =~ ^[0-9]+$ ]] \
    || (( max_cpu_percent > 100 )); then
    echo "Invalid build resource thresholds: NODE_PLANE_BUILD_MIN_MEM_MB must be an integer and NODE_PLANE_BUILD_MAX_CPU_PERCENT must be 0..100." >&2
    return 1
  fi
  if [[ ! -r /proc/meminfo || ! -r /proc/stat ]]; then
    echo "Cannot inspect available memory and CPU load; refusing local Rust build." >&2
    return 1
  fi
  mem_available_kb="$(awk '/^MemAvailable:/ {print $2; exit}' /proc/meminfo)"
  if [[ ! "$mem_available_kb" =~ ^[0-9]+$ ]]; then
    echo "Cannot determine available memory; refusing local Rust build." >&2
    return 1
  fi
  local mem_available_mb=$((mem_available_kb / 1024))
  if (( mem_available_mb < min_mem_mb )); then
    echo "Not enough free memory for local Rust build: ${mem_available_mb} MiB available; need at least ${min_mem_mb} MiB. Build release binaries on another machine and rerun with NODE_PLANE_BIN_SOURCE=release." >&2
    return 1
  fi

  read -r cpu_before idle_before < <(awk '$1 == "cpu" { idle=$5+$6; for (i=2; i<=NF; i++) total+=$i; print total, idle; exit }' /proc/stat)
  if [[ ! "$cpu_before" =~ ^[0-9]+$ || ! "$idle_before" =~ ^[0-9]+$ ]]; then
    echo "Cannot determine CPU load; refusing local Rust build." >&2
    return 1
  fi
  sleep 2
  read -r cpu_after idle_after < <(awk '$1 == "cpu" { idle=$5+$6; for (i=2; i<=NF; i++) total+=$i; print total, idle; exit }' /proc/stat)
  if [[ ! "$cpu_after" =~ ^[0-9]+$ || ! "$idle_after" =~ ^[0-9]+$ ]] \
    || (( cpu_after <= cpu_before || idle_after < idle_before )); then
    echo "Cannot measure CPU load; refusing local Rust build." >&2
    return 1
  fi
  cpu_busy_percent="$(awk -v total="$((cpu_after - cpu_before))" -v idle="$((idle_after - idle_before))" 'BEGIN { printf "%d", 100 * (total-idle) / total }')"
  if (( cpu_busy_percent > max_cpu_percent )); then
    echo "CPU is too busy for local Rust build: ${cpu_busy_percent}% busy; maximum is ${max_cpu_percent}%. Wait for the host to quiet down or build release binaries on another machine." >&2
    return 1
  fi
  echo "Build resource check passed: ${mem_available_mb} MiB available, CPU ${cpu_busy_percent}% busy."
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
      elif has_cmd cargo && has_cmd protoc; then
        echo "Release binaries are unavailable; building driver/agent from APP_ROOT=${APP_ROOT}."
        build_local_binaries
      else
        ensure_cargo_for_auto_build
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
      check_release_asset_access "$DRIVER_ASSET_NAME" "$driver_url" "$DRIVER_BIN_URL"
      check_release_asset_access "$AGENT_ASSET_NAME" "$agent_url" "$AGENT_BIN_URL"
      echo "Dry-run: release binary URLs are reachable."
      ;;
    build)
      need_cmd cargo
      echo "Dry-run: local cargo build mode is available."
      ;;
    auto)
      set_step "dry-run check release binary urls"
      if check_release_asset_access "$DRIVER_ASSET_NAME" "$driver_url" "$DRIVER_BIN_URL" \
        && check_release_asset_access "$AGENT_ASSET_NAME" "$agent_url" "$AGENT_BIN_URL"; then
        echo "Dry-run: release binary URLs are reachable (auto mode)."
      elif has_cmd cargo && has_cmd protoc; then
        echo "Dry-run: release binaries are unavailable; local cargo build is available (auto mode)."
      elif has_cmd apt-get || has_cmd dnf; then
        echo "Dry-run: release binaries are unavailable; Rust build tools require confirmation."
      else
        echo "Release binaries are unavailable and no supported Rust package manager was found." >&2
        return 1
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
    print("\x1f".join([
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
    if [[ -n "$ONLY_NODE_KEY" ]]; then
      echo "No enabled SSH-managed server found for --node-key ${ONLY_NODE_KEY}" >&2
      return 1
    fi
    echo "No SSH-managed enabled servers found; skipping node-agent deploy."
    return 0
  fi

  if [[ $DRY_RUN -eq 0 ]]; then
    set_step "prepare driver-agent mutual TLS certificates"
    prepare_driver_agent_tls
  fi

  local failed=0
  local matched=0
  local mappings=()
  local local_agent_sum=""
  if [[ $DRY_RUN -eq 0 ]]; then
    local_agent_sum="$(sha256_of_file "$agent_bin_path")"
  fi
  while IFS=$'\x1f' read -r server_key ssh_host ssh_port ssh_user ssh_key public_host; do
    [[ -z "$server_key" ]] && continue
    local target_host="$ssh_host"
    local target_user="$ssh_user"
    local target_key="$ssh_key"

    # Backward-compat normalization for older/dirty registry rows.
    # - If ssh_host already contains user@host, split it.
    # - If ssh_user accidentally contains a key path, treat it as ssh_key.
    if [[ "$target_host" == *"@"* ]]; then
      if [[ -z "$target_user" ]]; then
        target_user="${target_host%@*}"
      fi
      target_host="${target_host##*@}"
    fi
    # Treat path-like/key-like ssh_user as a misplaced key path and never as login.
    # Accept absolute/relative/tilde paths and common key filename patterns.
    if [[ "$target_user" == /* ]] || [[ "$target_user" == ~/* ]] || [[ "$target_user" == */* ]] || [[ "$target_user" == *".ssh"* ]] || [[ "$target_user" == id_* ]] || [[ "$target_user" == *.pem ]] || [[ "$target_user" == *.key ]]; then
      if [[ -z "$target_key" ]]; then
        target_key="$target_user"
      fi
      target_user=""
    fi

    local target="${target_user:+${target_user}@}${target_host}"
    local reach_host="${public_host:-$target_host}"
    if [[ "$reach_host" == *"@"* ]]; then
      reach_host="${reach_host##*@}"
    fi
    if [[ "$reach_host" == *:* ]]; then
      reach_host="${reach_host#\[}"
      reach_host="${reach_host%\]}"
      reach_host="[${reach_host}]"
      mappings+=("${server_key}=${reach_host}:${AGENT_PORT}")
    else
      mappings+=("${server_key}=${reach_host}:${AGENT_PORT}")
    fi

    if [[ -n "$ONLY_NODE_KEY" && "$server_key" != "$ONLY_NODE_KEY" ]]; then
      continue
    fi
    matched=$((matched + 1))

    echo
    echo "Deploying node-agent to ${server_key} (${target}:${ssh_port})..."

    local -a common_ssh_opts=("-o" "BatchMode=yes" "-o" "StrictHostKeyChecking=${SSH_STRICT_HOST_KEY_CHECKING:-accept-new}")
    if [[ -n "${SSH_KNOWN_HOSTS_PATH:-}" ]]; then
      common_ssh_opts+=("-o" "UserKnownHostsFile=${SSH_KNOWN_HOSTS_PATH}")
    fi
    if [[ -n "$target_key" ]]; then
      common_ssh_opts+=("-i" "$target_key")
    elif [[ -n "${SSH_KEY:-}" ]]; then
      common_ssh_opts+=("-i" "${SSH_KEY}")
    fi

    # Note: ssh uses -p for port, scp uses -P.
    local -a ssh_opts=("-p" "$ssh_port" "${common_ssh_opts[@]}")
    local -a scp_opts=("-P" "$ssh_port" "${common_ssh_opts[@]}")

    if [[ $DRY_RUN -eq 1 ]]; then
      if ! ssh "${ssh_opts[@]}" "$target" 'echo "node-plane-agent dry-run ok" >/dev/null'; then
        echo "Dry-run SSH check failed for ${server_key}" >&2
        failed=$((failed + 1))
      else
        echo "Dry-run SSH check passed for ${server_key}"
      fi
      continue
    fi

    set_step "prepare mutual TLS certificate for ${server_key}"
    if ! prepare_node_tls "$server_key" "$reach_host"; then
      failed=$((failed + 1))
      continue
    fi
    local remote_tls_dir quoted_remote_tls_dir
    if ! remote_tls_dir="$(ssh "${ssh_opts[@]}" "$target" 'umask 077; mktemp -d "${HOME}/.node-plane-agent-tls.XXXXXX"')"; then
      echo "Failed to create private TLS staging directory on ${server_key}" >&2
      failed=$((failed + 1))
      continue
    fi
    printf -v quoted_remote_tls_dir '%q' "$remote_tls_dir"
    if ! scp "${scp_opts[@]}" \
      "$TLS_CA_CERT" "$NODE_TLS_CERT" "$NODE_TLS_KEY" \
      "${target}:${remote_tls_dir}/"; then
      ssh "${ssh_opts[@]}" "$target" "rm -rf -- ${quoted_remote_tls_dir}" >/dev/null 2>&1 || true
      echo "Failed to transfer TLS material to ${server_key}" >&2
      failed=$((failed + 1))
      continue
    fi

    local tls_setup_script tls_changed
    tls_setup_script="$(mktemp)"
    cat > "$tls_setup_script" <<'TLSSETUP'
set -euo pipefail
stage="$1"
trap 'rm -rf -- "$stage"' EXIT
changed=0
sudo mkdir -p /etc/node-plane/tls
sudo chown root:root /etc/node-plane/tls
sudo chmod 0700 /etc/node-plane/tls
install_if_changed() {
  local name="$1" destination="$2" mode="$3"
  local source="${stage}/${name}"
  if ! sudo test -f "$destination" || ! sudo cmp -s "$source" "$destination"; then
    sudo install -o root -g root -m "$mode" "$source" "$destination"
    changed=1
  fi
  sudo chown root:root "$destination"
  sudo chmod "$mode" "$destination"
}
install_if_changed ca.crt /etc/node-plane/tls/ca.crt 0644
install_if_changed server.crt /etc/node-plane/tls/server.crt 0644
install_if_changed server.key /etc/node-plane/tls/server.key 0600
echo "$changed"
TLSSETUP
    if ! tls_changed="$(ssh "${ssh_opts[@]}" "$target" "bash -s -- ${quoted_remote_tls_dir}" < "$tls_setup_script")"; then
      echo "Failed to install mutual TLS material on ${server_key}" >&2
      failed=$((failed + 1))
      ssh "${ssh_opts[@]}" "$target" "rm -rf -- ${quoted_remote_tls_dir}" >/dev/null 2>&1 || true
      rm -f "$tls_setup_script"
      continue
    fi
    rm -f "$tls_setup_script"

    local remote_sum=""
    remote_sum="$(ssh "${ssh_opts[@]}" "$target" 'if [ -x /usr/local/bin/node-plane-agent ]; then sha256sum /usr/local/bin/node-plane-agent | awk "{print \$1}"; fi' 2>/dev/null || true)"
    # A matching binary alone is not enough: the node key, port or unit may
    # have changed since the last rollout.
    if [[ "$remote_sum" == "$local_agent_sum" ]] && ssh "${ssh_opts[@]}" "$target" \
      "sudo grep -Fxq 'node_key = \"${server_key}\"' /etc/node-plane/agent.toml && sudo grep -Fxq 'listen_addr = \"0.0.0.0:${AGENT_PORT}\"' /etc/node-plane/agent.toml && sudo grep -Fxq 'Environment=NODE_AGENT_CONFIG_PATH=/etc/node-plane/agent.toml' /etc/systemd/system/node-plane-agent.service" >/dev/null 2>&1; then
      set_step "restart node-agent on ${server_key}"
      if ssh "${ssh_opts[@]}" "$target" 'sudo systemctl is-active --quiet node-plane-agent' >/dev/null 2>&1; then
        if [[ "$tls_changed" == "1" ]]; then
          if ssh "${ssh_opts[@]}" "$target" 'sudo systemctl restart node-plane-agent && sudo systemctl is-active --quiet node-plane-agent' >/dev/null 2>&1; then
            echo "node-agent certificates rotated and service restarted on ${server_key}."
            continue
          fi
          echo "node-agent restart after certificate update failed on ${server_key}" >&2
          show_agent_service_diagnostics "$target" "${ssh_opts[@]}"
          failed=$((failed + 1))
          continue
        else
          echo "node-agent is up to date and active on ${server_key}; skipping reinstall."
          continue
        fi
      fi
      if ssh "${ssh_opts[@]}" "$target" 'sudo systemctl restart node-plane-agent && sudo systemctl is-active --quiet node-plane-agent' >/dev/null 2>&1; then
        echo "node-agent binary unchanged; service restarted on ${server_key}."
        continue
      fi
      echo "node-agent service restart failed on ${server_key}" >&2
      show_agent_service_diagnostics "$target" "${ssh_opts[@]}"
      failed=$((failed + 1))
      continue
    fi

    set_step "install node-agent on ${server_key}"
    if ! scp "${scp_opts[@]}" "$agent_bin_path" "${target}:/tmp/node-plane-agent"; then
      echo "Failed to copy agent binary to ${server_key}" >&2
      failed=$((failed + 1))
      continue
    fi

    local agent_link_check
    if ! agent_link_check="$(ssh "${ssh_opts[@]}" "$target" 'ldd /tmp/node-plane-agent 2>&1 || true')"; then
      echo "Failed to check node-agent binary compatibility on ${server_key}" >&2
      failed=$((failed + 1))
      continue
    fi
    if [[ "$agent_link_check" == *"not found"* ]]; then
      echo "node-agent binary is incompatible with ${server_key}; keeping the previously installed binary." >&2
      echo "$agent_link_check" >&2
      ssh "${ssh_opts[@]}" "$target" 'rm -f /tmp/node-plane-agent' >/dev/null 2>&1 || true
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
tls_certificate_path = "/etc/node-plane/tls/server.crt"
tls_key_path = "/etc/node-plane/tls/server.key"
tls_client_ca_path = "/etc/node-plane/tls/ca.crt"
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
      show_agent_service_diagnostics "$target" "${ssh_opts[@]}"
      failed=$((failed + 1))
      rm -f "$remote_script"
      continue
    fi
    rm -f "$remote_script"
    echo "node-agent is active on ${server_key}"
  done <<< "$lines"

  if [[ -n "$ONLY_NODE_KEY" && $matched -eq 0 ]]; then
    echo "No enabled SSH-managed server found for --node-key ${ONLY_NODE_KEY}" >&2
    return 1
  fi

  if [[ $STRICT_MODE -eq 1 && $failed -gt 0 ]]; then
    echo "node-agent deploy failures: ${failed}" >&2
    return 1
  fi

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
      if set_env_value_if_changed "$ENV_FILE" "NODE_AGENT_CA_CERT" "$TLS_CA_CERT"; then ENV_CHANGED=1; fi
      if set_env_value_if_changed "$ENV_FILE" "NODE_AGENT_CLIENT_CERT" "$TLS_CLIENT_CERT"; then ENV_CHANGED=1; fi
      if set_env_value_if_changed "$ENV_FILE" "NODE_AGENT_CLIENT_KEY" "$TLS_CLIENT_KEY"; then ENV_CHANGED=1; fi
      echo "Configured NODE_AGENT_TARGETS in ${ENV_FILE}: ${mapping_csv}"
    fi
  fi

  if [[ $failed -gt 0 ]]; then
    if [[ $STRICT_MODE -eq 1 ]]; then
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
