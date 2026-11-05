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
BACKEND_NODE_KEY=""
BACKEND_NODE_MODE=""
BIN_SOURCE="${NODE_PLANE_BIN_SOURCE:-auto}" # auto|release
BIN_SOURCE_EXPLICIT=0
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
DRIVER_SHA256="${NODE_PLANE_DRIVER_SHA256:-}"
AGENT_SHA256="${NODE_PLANE_AGENT_SHA256:-}"
CURRENT_STEP="startup"
DRIVER_BIN_CHANGED=0
DRIVER_UNIT_CHANGED=0
ENV_CHANGED=0
CONTROLLER_ID=""
RETIRED_NODE_KEYS=""
JOURNAL_ARCHIVE_NOTICES=()

set_step() {
  CURRENT_STEP="$1"
  if [[ -n "${NODE_PLANE_AGENT_PROGRESS_FILE:-}" && -n "${NODE_PLANE_AGENT_PROGRESS_ID:-}" ]]; then
    PYTHONPATH="${APP_ROOT}/app" "${PYTHON_BIN:-python3}" -m backend.installation_progress \
      "$NODE_PLANE_AGENT_PROGRESS_FILE" "$NODE_PLANE_AGENT_PROGRESS_ID" "$CURRENT_STEP" 2>/dev/null || true
  fi
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

generate_local_driver_token() {
  if has_cmd openssl; then
    openssl rand -hex 32
  else
    od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
  fi
}

ensure_local_driver_token() {
  local token
  token="$(read_env_value NODE_PLANE_DRIVER_LOCAL_TOKEN "$ENV_FILE")"
  if [[ -n "$token" ]]; then
    export NODE_PLANE_DRIVER_LOCAL_TOKEN="$token"
    return 0
  fi
  if [[ $DRY_RUN -eq 1 ]]; then
    return 0
  fi
  token="$(generate_local_driver_token)"
  [[ "$token" =~ ^[0-9a-f]{64}$ ]] || {
    echo "Failed to generate local Driver authentication token." >&2
    return 1
  }
  set_env_value "$ENV_FILE" NODE_PLANE_DRIVER_LOCAL_TOKEN "$token"
  chmod 0600 "$ENV_FILE"
  export NODE_PLANE_DRIVER_LOCAL_TOKEN="$token"
  ENV_CHANGED=1
}

sha256_of_file() {
  local path="$1"
  sha256sum "$path" | awk '{print $1}'
}

download_to_file() {
  local url="$1"
  local out="$2"
  local max_bytes="${3:-}"
  if [[ "$url" != https://* ]]; then
    echo "Insecure download URL (HTTPS required)" >&2
    return 1
  fi
  if has_cmd curl; then
    local -a curl_opts=("-fsSL" "--proto" "=https" "--proto-redir" "=https" "--connect-timeout" "15" "--max-time" "120")
    if [[ -n "$max_bytes" ]]; then
      curl_opts+=("--max-filesize" "$max_bytes")
    fi
    if curl "${curl_opts[@]}" "$url" -o "$out"; then
      if [[ -n "$max_bytes" && -f "$out" ]]; then
        local file_size
        file_size="$(wc -c < "$out")"
        if (( file_size > max_bytes )); then
          echo "Downloaded file exceeds maximum size limit ($max_bytes bytes)" >&2
          rm -f "$out"
          return 1
        fi
      fi
      return 0
    fi
    rm -f "$out"
    return 1
  fi
  echo "curl is required for HTTPS-only release downloads." >&2
  return 1
}

check_url_access() {
  local url="$1"
  if [[ "$url" != https://* ]]; then
    echo "Insecure check URL (HTTPS required)" >&2
    return 1
  fi
  if has_cmd curl; then
    curl -fsSLI --proto '=https' --proto-redir '=https' --connect-timeout 15 --max-time 30 "$url" >/dev/null
    return $?
  fi
  echo "curl is required for HTTPS-only release checks." >&2
  return 1
}

release_asset_api_url() {
  local asset_name="$1"
  local metadata="${WORK_DIR}/release.metadata.json"
  if [[ ! -s "$metadata" ]]; then
    download_to_file "https://api.github.com/repos/${GITHUB_REPO}/releases/tags/$(detect_release_ref)" "$metadata" "1048576" || return 1
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
  local asset_name="$1" direct_url="$2" out="$3" custom_url="$4" max_bytes="${5:-}" api_url
  if download_to_file "$direct_url" "$out" "$max_bytes"; then
    return 0
  fi
  [[ -z "$custom_url" ]] || return 1
  api_url="$(release_asset_api_url "$asset_name")" || return 1
  echo "Direct download unavailable; trying GitHub API for ${asset_name}."
  if [[ "$api_url" != https://* ]]; then
    echo "Insecure API asset URL (HTTPS required)" >&2
    rm -f "$out"
    return 1
  fi
  if has_cmd curl; then
    local -a curl_opts=("-fsSL" "--proto" "=https" "--proto-redir" "=https" "--connect-timeout" "15" "--max-time" "120" "-H" "Accept: application/octet-stream")
    if [[ -n "$max_bytes" ]]; then
      curl_opts+=("--max-filesize" "$max_bytes")
    fi
    if curl "${curl_opts[@]}" "$api_url" -o "$out"; then
      if [[ -n "$max_bytes" && -f "$out" ]]; then
        local file_size
        file_size="$(wc -c < "$out")"
        if (( file_size > max_bytes )); then
          rm -f "$out"
          return 1
        fi
      fi
      return 0
    fi
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
    auto|release) ;;
    build)
      echo "Build mode is disabled: on-server source builds are no longer supported. Use release binaries." >&2
      exit 1
      ;;
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
    --backend-node-key)
      BACKEND_NODE_KEY="${2:-}"
      shift 2
      ;;
    --backend-local)
      BACKEND_NODE_MODE="local"
      shift
      ;;
    --bin-source)
      BIN_SOURCE="${2:-}"
      BIN_SOURCE_EXPLICIT=1
      shift 2
      ;;
    --bin-source=*)
      BIN_SOURCE="${1#*=}"
      BIN_SOURCE_EXPLICIT=1
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage:
  scripts/setup_driver_agents.sh [--skip-driver] [--skip-agents] [--node-key KEY] [--agent-port 50061] [--strict] [--dry-run] [--bin-source auto|release]
  scripts/setup_driver_agents.sh --backend-node-key KEY --backend-local [--bin-source release]

Purpose:
  - Install local node-plane-driver as a systemd service.
  - Deploy node-plane-agent to the controller's own node from the server registry.
    Remote servers install their agent with the one-time install command and
    are never reached over SSH from here; their routes are kept.
  - Backend mode installs the agent for an existing local backend node.
  - Write NODE_AGENT_TARGETS and driver mTLS settings in the shared .env.

Binary source modes:
  auto     Use GitHub release binaries (source builds are disabled).
  release  Use GitHub release binaries only.

Dry run:
  --dry-run validates binary source resolution and local registration.
  It does not install binaries, write systemd units, or restart services.

Key env overrides:
  NODE_PLANE_BIN_SOURCE             auto|release (default: auto)
  NODE_PLANE_GITHUB_REPO            owner/repo (default: saharoktyan/node-plane)
  NODE_PLANE_BINARY_RELEASE         release tag (default: v<VERSION> from app root)
  NODE_PLANE_DRIVER_ASSET_NAME      driver asset filename
  NODE_PLANE_AGENT_ASSET_NAME       agent asset filename
  NODE_PLANE_DRIVER_BIN_URL         explicit driver binary URL
  NODE_PLANE_AGENT_BIN_URL          explicit agent binary URL
  NODE_PLANE_DRIVER_SHA256          explicit driver binary archive SHA-256 digest
  NODE_PLANE_AGENT_SHA256           explicit agent binary archive SHA-256 digest
EOF
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [[ -n "$BACKEND_NODE_KEY" || -n "$BACKEND_NODE_MODE" ]]; then
  if [[ ! "$BACKEND_NODE_KEY" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]]; then
    echo "Backend node key must match the backend inventory key format." >&2
    exit 1
  fi
  if [[ "$BACKEND_NODE_MODE" != local ]]; then
    # Remote servers install their agent with the one-time install command.
    echo "Backend onboarding needs --backend-local; remote servers use the agent install command." >&2
    exit 1
  fi
  if [[ -n "$ONLY_NODE_KEY" || $SKIP_AGENTS -eq 1 ]]; then
    echo "Backend onboarding cannot combine with --node-key or --skip-agents." >&2
    exit 1
  fi
  ONLY_NODE_KEY="$BACKEND_NODE_KEY"
  STRICT_MODE=1
fi

ensure_bin_source_mode

if [[ ! "$AGENT_PORT" =~ ^[0-9]+$ ]]; then
  echo "Invalid --agent-port: $AGENT_PORT" >&2
  exit 1
fi

need_cmd python3
need_cmd sudo

validate_security_configuration() {
  if [[ -z "$DRIVER_SHA256" && -n "${NODE_PLANE_DRIVER_SHA256:-}" ]]; then
    DRIVER_SHA256="$NODE_PLANE_DRIVER_SHA256"
  fi
  if [[ -z "$AGENT_SHA256" && -n "${NODE_PLANE_AGENT_SHA256:-}" ]]; then
    AGENT_SHA256="$NODE_PLANE_AGENT_SHA256"
  fi
  if [[ -n "$DRIVER_BIN_URL" ]]; then
    if [[ "$DRIVER_BIN_URL" != https://* ]]; then
      echo "Insecure driver binary URL (HTTPS required)" >&2
      exit 1
    fi
    if [[ -z "$DRIVER_SHA256" ]]; then
      echo "Explicit custom driver URL requires matching explicit NODE_PLANE_DRIVER_SHA256 override." >&2
      exit 1
    fi
    if [[ ! "$DRIVER_SHA256" =~ ^[0-9a-fA-F]{64}$ ]]; then
      echo "Invalid NODE_PLANE_DRIVER_SHA256: must be 64 hexadecimal characters." >&2
      exit 1
    fi
  fi
  if [[ -n "$AGENT_BIN_URL" ]]; then
    if [[ "$AGENT_BIN_URL" != https://* ]]; then
      echo "Insecure agent binary URL (HTTPS required)" >&2
      exit 1
    fi
    if [[ -z "$AGENT_SHA256" ]]; then
      echo "Explicit custom agent URL requires matching explicit NODE_PLANE_AGENT_SHA256 override." >&2
      exit 1
    fi
    if [[ ! "$AGENT_SHA256" =~ ^[0-9a-fA-F]{64}$ ]]; then
      echo "Invalid NODE_PLANE_AGENT_SHA256: must be 64 hexadecimal characters." >&2
      exit 1
    fi
  fi
  if [[ -n "$DRIVER_SHA256" && ! "$DRIVER_SHA256" =~ ^[0-9a-fA-F]{64}$ ]]; then
    echo "Invalid NODE_PLANE_DRIVER_SHA256: must be 64 hexadecimal characters." >&2
    exit 1
  fi
  if [[ -n "$AGENT_SHA256" && ! "$AGENT_SHA256" =~ ^[0-9a-fA-F]{64}$ ]]; then
    echo "Invalid NODE_PLANE_AGENT_SHA256: must be 64 hexadecimal characters." >&2
    exit 1
  fi
}

# The env file is systemd's EnvironmentFile: KEY=VALUE lines, values taken
# literally (the installer writes them unquoted, such as an administrator's
# name with spaces), so it is read line by line instead of sourced.
load_env_file() {
  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    if [[ ${#value} -ge 2 && ( ( "${value:0:1}" == '"' && "${value: -1}" == '"' ) || ( "${value:0:1}" == "'" && "${value: -1}" == "'" ) ) ]]; then
      value="${value:1:${#value}-2}"
    fi
    export "${key}=${value}"
  done < "$1"
}

if [[ -f "$ENV_FILE" ]]; then
  load_env_file "$ENV_FILE"
  if [[ ${BIN_SOURCE_EXPLICIT:-0} -eq 0 && -n "${NODE_PLANE_BIN_SOURCE:-}" ]]; then
    BIN_SOURCE="$NODE_PLANE_BIN_SOURCE"
  fi
  ensure_bin_source_mode
fi
validate_security_configuration

# Use the same controller identity as config.SSH_KEY and the backend key endpoint.
# Older installations can have no SSH_KEY entry, or an empty one. Persist only
# that default so the driver's EnvironmentFile cannot blank its unit fallback.
if [[ -z "${SSH_KEY:-}" ]]; then
  SSH_KEY="${SHARED_ROOT}/ssh/id_ed25519"
  if [[ $DRY_RUN -eq 0 ]]; then
    if set_env_value_if_changed "$ENV_FILE" "SSH_KEY" "$SSH_KEY"; then ENV_CHANGED=1; fi
  fi
fi
export SSH_KEY

if [[ $SKIP_DRIVER -eq 0 ]]; then
  ensure_local_driver_token
fi

APP_ROOT="$(cd "$APP_ROOT" && pwd)"

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
  local driver_archive="${WORK_DIR}/driver.asset.tar.gz"
  local agent_archive="${WORK_DIR}/agent.asset.tar.gz"
  local max_archive_size=268435456 # 256 MiB
  local max_sums_size=1048576     # 1 MiB

  set_step "download driver binary"
  if ! download_release_asset "$DRIVER_ASSET_NAME" "$driver_url" "$driver_archive" "$DRIVER_BIN_URL" "$max_archive_size"; then
    echo "RELEASE_BINARY_UNAVAILABLE: Driver release asset ${DRIVER_ASSET_NAME} for $(detect_release_ref) is missing or inaccessible. Publish both release assets or configure authenticated binary URLs." >&2
    return 1
  fi

  set_step "download agent binary"
  if ! download_release_asset "$AGENT_ASSET_NAME" "$agent_url" "$agent_archive" "$AGENT_BIN_URL" "$max_archive_size"; then
    echo "RELEASE_BINARY_UNAVAILABLE: Agent release asset ${AGENT_ASSET_NAME} for $(detect_release_ref) is missing or inaccessible. Publish both release assets or configure authenticated binary URLs." >&2
    return 1
  fi

  local driver_checksum="$DRIVER_SHA256"
  local agent_checksum="$AGENT_SHA256"
  local sums_file="${WORK_DIR}/SHA256SUMS.txt"

  if [[ -z "$driver_checksum" || -z "$agent_checksum" ]]; then
    set_step "download release checksums"
    local sums_url="$(asset_url "SHA256SUMS.txt")"
    if ! download_release_asset "SHA256SUMS.txt" "$sums_url" "$sums_file" "" "$max_sums_size"; then
      echo "RELEASE_BINARY_UNAVAILABLE: Release checksum asset SHA256SUMS.txt for $(detect_release_ref) is missing or inaccessible. Publish release checksums or configure authenticated binary URLs." >&2
      return 1
    fi
  fi

  if [[ -z "$driver_checksum" ]]; then
    driver_checksum="$("$PYTHON_BIN" "${SCRIPT_DIR}/verify_server_artifacts.py" parse-checksum --sums-file "$sums_file" --asset-name "$DRIVER_ASSET_NAME")" || {
      echo "RELEASE_BINARY_UNAVAILABLE: Failed to parse driver checksum for ${DRIVER_ASSET_NAME} from SHA256SUMS.txt" >&2
      return 1
    }
  fi

  if [[ -z "$agent_checksum" ]]; then
    agent_checksum="$("$PYTHON_BIN" "${SCRIPT_DIR}/verify_server_artifacts.py" parse-checksum --sums-file "$sums_file" --asset-name "$AGENT_ASSET_NAME")" || {
      echo "RELEASE_BINARY_UNAVAILABLE: Failed to parse agent checksum for ${AGENT_ASSET_NAME} from SHA256SUMS.txt" >&2
      return 1
    }
  fi

  local driver_stage="${WORK_DIR}/stage-driver"
  local agent_stage="${WORK_DIR}/stage-agent"
  mkdir -m 0700 -p "$driver_stage" "$agent_stage"

  local driver_out="${driver_stage}/node-plane-driver"
  local agent_out="${agent_stage}/node-plane-agent"

  set_step "verify both server archives before extraction"
  if ! "$PYTHON_BIN" "${SCRIPT_DIR}/verify_server_artifacts.py" verify-archive \
      --component driver --archive "$driver_archive" --expected-sha256 "$driver_checksum"; then
    echo "RELEASE_BINARY_UNAVAILABLE: Driver release artifact verification failed for ${DRIVER_ASSET_NAME}." >&2
    return 1
  fi
  if ! "$PYTHON_BIN" "${SCRIPT_DIR}/verify_server_artifacts.py" verify-archive \
      --component agent --archive "$agent_archive" --expected-sha256 "$agent_checksum"; then
    echo "RELEASE_BINARY_UNAVAILABLE: Agent release artifact verification failed for ${AGENT_ASSET_NAME}." >&2
    return 1
  fi

  set_step "verify and unpack driver binary"
  if ! "$PYTHON_BIN" "${SCRIPT_DIR}/verify_server_artifacts.py" verify-and-extract \
      --component driver \
      --archive "$driver_archive" \
      --expected-sha256 "$driver_checksum" \
      --output "$driver_out"; then
    echo "RELEASE_BINARY_UNAVAILABLE: Driver release artifact verification failed for ${DRIVER_ASSET_NAME}." >&2
    return 1
  fi

  set_step "verify and unpack agent binary"
  if ! "$PYTHON_BIN" "${SCRIPT_DIR}/verify_server_artifacts.py" verify-and-extract \
      --component agent \
      --archive "$agent_archive" \
      --expected-sha256 "$agent_checksum" \
      --output "$agent_out"; then
    echo "RELEASE_BINARY_UNAVAILABLE: Agent release artifact verification failed for ${AGENT_ASSET_NAME}." >&2
    return 1
  fi

  driver_bin_path="$driver_out"
  agent_bin_path="$agent_out"
}

resolve_binaries() {
  case "$BIN_SOURCE" in
    auto|release)
      download_release_binaries
      ;;
    build)
      echo "Build mode is disabled: on-server source builds are no longer supported. Use release binaries." >&2
      exit 1
      ;;
    *)
      echo "Unsupported binary source mode: ${BIN_SOURCE}" >&2
      exit 1
      ;;
  esac
  [[ -x "$driver_bin_path" ]] || { echo "Driver binary is not executable: $driver_bin_path" >&2; exit 1; }
  [[ -x "$agent_bin_path" ]] || { echo "Agent binary is not executable: $agent_bin_path" >&2; exit 1; }
}

resolve_binaries_dry_run() {
  local driver_url="${DRIVER_BIN_URL:-$(asset_url "$DRIVER_ASSET_NAME")}"
  local agent_url="${AGENT_BIN_URL:-$(asset_url "$AGENT_ASSET_NAME")}"
  case "$BIN_SOURCE" in
    auto|release)
      set_step "dry-run check release binary urls"
      if ! check_release_asset_access "$DRIVER_ASSET_NAME" "$driver_url" "$DRIVER_BIN_URL"; then
        echo "RELEASE_BINARY_UNAVAILABLE: Driver release asset ${DRIVER_ASSET_NAME} for $(detect_release_ref) is missing or inaccessible." >&2
        return 1
      fi
      if ! check_release_asset_access "$AGENT_ASSET_NAME" "$agent_url" "$AGENT_BIN_URL"; then
        echo "RELEASE_BINARY_UNAVAILABLE: Agent release asset ${AGENT_ASSET_NAME} for $(detect_release_ref) is missing or inaccessible." >&2
        return 1
      fi
      if [[ -z "$DRIVER_BIN_URL" || -z "$AGENT_BIN_URL" ]]; then
        local sums_url="$(asset_url "SHA256SUMS.txt")"
        if ! check_release_asset_access "SHA256SUMS.txt" "$sums_url" ""; then
          echo "RELEASE_BINARY_UNAVAILABLE: Release checksum asset SHA256SUMS.txt for $(detect_release_ref) is missing or inaccessible." >&2
          return 1
        fi
      fi
      echo "Dry-run: release binary URLs are reachable."
      ;;
    build)
      echo "Build mode is disabled: on-server source builds are no longer supported. Use release binaries." >&2
      return 1
      ;;
    *)
      echo "Unsupported binary source mode: ${BIN_SOURCE}" >&2
      return 1
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
Environment=SSH_KEY=${SSH_KEY}
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

list_managed_servers_tsv() {
  if [[ -n "$BACKEND_NODE_KEY" ]]; then
    # Trusted local confirmation only.
    PYTHONPATH="${APP_ROOT}/app" NODE_PLANE_APP_DIR="${APP_ROOT}" NODE_PLANE_SHARED_DIR="${SHARED_ROOT}" \
      "$PYTHON_BIN" - "$BACKEND_NODE_KEY" <<'PY'
import sys
from db import get_db
with get_db().connect() as conn:
    row = conn.execute('SELECT key FROM backend_nodes WHERE key = ?', (sys.argv[1],)).fetchone()
if row is None:
    raise SystemExit('Backend node is absent; create its draft before onboarding the agent')
PY
    printf '%s\x1flocal\x1f\x1f\x1f\x1f\x1f\n' "$BACKEND_NODE_KEY"
    return
  fi
  PYTHONPATH="${APP_ROOT}/app" NODE_PLANE_APP_DIR="${APP_ROOT}" NODE_PLANE_SHARED_DIR="${SHARED_ROOT}" "$PYTHON_BIN" - <<'PY'
import json
from db import get_db

with get_db().connect() as conn:
    servers = conn.execute('''SELECT n.key, n.settings_json, c.transport, c.ssh_target
        FROM backend_nodes n JOIN backend_node_connections c ON c.node_key = n.key
        WHERE n.enabled = 1 AND NOT EXISTS (
            SELECT 1 FROM backend_node_drains d WHERE d.node_key = n.key)
        ORDER BY n.key''').fetchall()
if sum(server['transport'] == "local" for server in servers) > 1:
    raise SystemExit("Only one local node can use the controller's node-agent service.")
for srv in servers:
    if srv['transport'] == "local":
        print("\x1f".join([srv['key'], "local", "", "", "", "", ""]))
        continue
    if not srv['ssh_target']:
        continue
    ssh_host = srv['ssh_target'].strip()
    ssh_user = ""
    ssh_key = ""
    public_host = json.loads(srv['settings_json']).get('public_host') or ssh_host.rsplit('@', 1)[-1]
    if not ssh_host:
        continue
    print("\x1f".join([
    srv['key'],
    "ssh",
        ssh_host,
        "22",
        ssh_user,
        ssh_key,
        public_host,
    ]))
PY
}

# Speed limits shape with tc and nft and enter the AWG container with
# nsenter. Installed when missing; without them a node only reports that
# it cannot limit speed, so a failure here does not stop the agent install.
SHAPING_TOOLS_SCRIPT='missing=""
command -v tc >/dev/null 2>&1 || missing="$missing iproute2"
command -v nft >/dev/null 2>&1 || missing="$missing nftables"
command -v nsenter >/dev/null 2>&1 || missing="$missing util-linux"
if [ -n "$missing" ]; then
  if command -v apt-get >/dev/null 2>&1 \
    && { sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y $missing >/dev/null 2>&1 \
      || { sudo apt-get update >/dev/null 2>&1 \
        && sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y $missing >/dev/null 2>&1; }; }; then
    echo "Installed for speed limits:$missing"
  else
    echo "Speed limits need these packages on the node:$missing" >&2
  fi
fi
'

install_local_agent() {
  local server_key="$1" expected_sum="$2" current_sum="" changed=0
  local config_tmp unit_tmp
  local archive_notice
  set_step "check previous controller journals for local node ${server_key}"
  archive_notice="$(sudo python3 "${SCRIPT_DIR}/lib/archive_agent_journals.py" \
    "$CONTROLLER_ID" "$(sha256_of_file "$TLS_CA_CERT")" "$server_key" "$RETIRED_NODE_KEYS")" || return 1
  if [[ -n "$archive_notice" ]]; then
    JOURNAL_ARCHIVE_NOTICES+=("$archive_notice")
    echo "$archive_notice"
  else
    set_step "skip previous controller journals for local node ${server_key}"
  fi
  set_step "prepare mutual TLS certificate for local node ${server_key}"
  prepare_node_tls "$server_key" "127.0.0.1" || return 1

  set_step "install node-agent on local node ${server_key}"
  bash -c "$SHAPING_TOOLS_SCRIPT" || true
  if sudo test -x /usr/local/bin/node-plane-agent; then
    current_sum="$(sudo sha256sum /usr/local/bin/node-plane-agent | awk '{print $1}')"
  fi
  if [[ "$current_sum" != "$expected_sum" ]]; then
    sudo install -m 0755 "$agent_bin_path" /usr/local/bin/node-plane-agent || return 1
    changed=1
  fi

  sudo mkdir -p /etc/node-plane/tls || return 1
  sudo chmod 0700 /etc/node-plane/tls || return 1
  local source destination mode
  for source in "$TLS_CA_CERT" "$NODE_TLS_CERT" "$NODE_TLS_KEY"; do
    case "$source" in
      "$TLS_CA_CERT") destination=/etc/node-plane/tls/ca.crt; mode=0644 ;;
      "$NODE_TLS_CERT") destination=/etc/node-plane/tls/server.crt; mode=0644 ;;
      *) destination=/etc/node-plane/tls/server.key; mode=0600 ;;
    esac
    if ! sudo test -f "$destination" || ! sudo cmp -s "$source" "$destination"; then
      sudo install -o root -g root -m "$mode" "$source" "$destination" || return 1
      changed=1
    fi
    sudo chmod "$mode" "$destination" || return 1
  done

  config_tmp="$(mktemp)"
  unit_tmp="$(mktemp)"
  cat > "$config_tmp" <<EOF
node_key = "${server_key}"
listen_addr = "127.0.0.1:${AGENT_PORT}"
tls_certificate_path = "/etc/node-plane/tls/server.crt"
tls_key_path = "/etc/node-plane/tls/server.key"
tls_client_ca_path = "/etc/node-plane/tls/ca.crt"
EOF
  cat > "$unit_tmp" <<EOF
[Unit]
Description=Node Plane Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=NODE_AGENT_CONFIG_PATH=/etc/node-plane/agent.toml
Environment=NODE_AGENT_NODE_KEY=${server_key}
Environment=NODE_AGENT_LISTEN_ADDR=127.0.0.1:${AGENT_PORT}
ExecStart=/usr/local/bin/node-plane-agent
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  if ! sudo test -f /etc/node-plane/agent.toml || ! sudo cmp -s "$config_tmp" /etc/node-plane/agent.toml; then
    sudo install -o root -g root -m 0600 "$config_tmp" /etc/node-plane/agent.toml || return 1
    changed=1
  fi
  if ! sudo test -f /etc/systemd/system/node-plane-agent.service || ! sudo cmp -s "$unit_tmp" /etc/systemd/system/node-plane-agent.service; then
    sudo install -o root -g root -m 0644 "$unit_tmp" /etc/systemd/system/node-plane-agent.service || return 1
    sudo systemctl daemon-reload || return 1
    changed=1
  fi
  rm -f "$config_tmp" "$unit_tmp"
  # The agent owns its systemd hardening drop-in; see docs/agent.md.
  local hardening
  hardening="$(sudo timeout 30 /usr/local/bin/node-plane-agent harden)" || return 1
  [[ "$hardening" != *written* ]] || changed=1
  sudo systemctl enable --now node-plane-agent || return 1
  if [[ $changed -eq 1 ]]; then
    sudo systemctl restart node-plane-agent || return 1
  fi
  if ! sudo systemctl is-active --quiet node-plane-agent; then
    sudo systemctl status node-plane-agent --no-pager -l >&2 || true
    sudo journalctl -u node-plane-agent -n 30 --no-pager >&2 || true
    return 1
  fi
  echo "node-agent is active on local node ${server_key}"
}

deploy_agents() {
  local lines
  if ! lines="$(list_managed_servers_tsv)"; then
    echo "Failed to query server registry from APP_ROOT=${APP_ROOT} using ${PYTHON_BIN}" >&2
    return 1
  fi
  if [[ -z "$lines" ]]; then
    if [[ -n "$ONLY_NODE_KEY" ]]; then
      echo "No enabled managed server found for --node-key ${ONLY_NODE_KEY}" >&2
      return 1
    fi
    echo "No enabled managed servers found; skipping node-agent deploy."
    return 0
  fi

  if [[ $DRY_RUN -eq 0 ]]; then
    set_step "prepare driver-agent mutual TLS certificates"
    prepare_driver_agent_tls
    local identity_snapshot
    identity_snapshot="$(PYTHONPATH="${APP_ROOT}/app" NODE_PLANE_APP_DIR="${APP_ROOT}" \
      NODE_PLANE_SHARED_DIR="${SHARED_ROOT}" "$PYTHON_BIN" - <<'PY'
import re
from db import get_db
from backend.installation_identity import controller_identity
from uuid import UUID
db = get_db()
identity = controller_identity(db)
if str(UUID(identity)) != identity:
    raise SystemExit('Invalid controller identity')
with db.connect() as conn:
    retired = [row['node_key'] for row in conn.execute('''SELECT r.node_key FROM backend_node_retirements r
        WHERE NOT EXISTS (SELECT 1 FROM backend_nodes n WHERE n.key=r.node_key) ORDER BY r.node_key''').fetchall()]
if any(not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', key) for key in retired):
    raise SystemExit('Invalid retired node key')
print(identity)
print(','.join(retired))
PY
    )" || return 1
    CONTROLLER_ID="${identity_snapshot%%$'\n'*}"
    RETIRED_NODE_KEYS=""
    if [[ "$identity_snapshot" == *$'\n'* ]]; then
      RETIRED_NODE_KEYS="${identity_snapshot#*$'\n'}"
    fi
  fi

  local failed=0
  local matched=0
  local mappings=()
  local local_mappings=()
  local local_agent_sum=""
  if [[ $DRY_RUN -eq 0 ]]; then
    local_agent_sum="$(sha256_of_file "$agent_bin_path")"
  fi
  while IFS=$'\x1f' read -r server_key transport ssh_host ssh_port ssh_user ssh_key public_host; do
    [[ -z "$server_key" ]] && continue
    if [[ "$transport" == "local" ]]; then
      mappings+=("${server_key}=127.0.0.1:${AGENT_PORT}")
      local_mappings+=("${server_key}=127.0.0.1:${AGENT_PORT}")
      if [[ -n "$ONLY_NODE_KEY" && "$server_key" != "$ONLY_NODE_KEY" ]]; then
        continue
      fi
      matched=$((matched + 1))
      echo
      echo "Deploying node-agent to local node ${server_key}..."
      if [[ $DRY_RUN -eq 1 ]]; then
        echo "Dry-run local agent check passed for ${server_key}"
      elif ! install_local_agent "$server_key" "$local_agent_sum"; then
        failed=$((failed + 1))
      fi
      continue
    fi
    # A remote server installs and updates its agent itself (the one-time
    # install command, then signed self-update); only its route stays here.
    local reach_host="${public_host:-${ssh_host##*@}}"
    reach_host="${reach_host##*@}"
    if [[ "$reach_host" == *:* ]]; then
      reach_host="${reach_host#\[}"
      reach_host="${reach_host%\]}"
      reach_host="[${reach_host}]"
    fi
    mappings+=("${server_key}=${reach_host}:${AGENT_PORT}")
  done <<< "$lines"

  if [[ -n "$ONLY_NODE_KEY" && $matched -eq 0 ]]; then
    echo "No enabled managed server found for --node-key ${ONLY_NODE_KEY}" >&2
    return 1
  fi

  if [[ $STRICT_MODE -eq 1 && $failed -gt 0 ]]; then
    echo "node-agent deploy failures: ${failed}" >&2
    return 1
  fi

  if [[ ${#mappings[@]} -gt 0 ]]; then
    local mapping_csv
    if [[ -n "$ONLY_NODE_KEY" ]]; then
      # A targeted rollout must not erase routes for agents that were set up
      # earlier if the current registry query omits them temporarily.
      local previous_mapping old_mapping old_key new_mapping
      local -a previous_mappings=()
      local -A mapped_keys=()
      for new_mapping in "${mappings[@]}"; do
        mapped_keys["${new_mapping%%=*}"]=1
      done
      previous_mapping="$(read_env_value "NODE_AGENT_TARGETS" "$ENV_FILE")"
      IFS=',' read -r -a previous_mappings <<< "$previous_mapping"
      for old_mapping in "${previous_mappings[@]}"; do
        [[ "$old_mapping" == *=* ]] || continue
        old_key="${old_mapping%%=*}"
        [[ -n "$old_key" && -n "${old_mapping#*=}" ]] || continue
        if [[ -z "${mapped_keys[$old_key]+x}" ]]; then
          mappings+=("$old_mapping")
          mapped_keys["$old_key"]=1
        fi
      done
    fi
    mapping_csv="$(IFS=,; echo "${mappings[*]}")"
    if [[ $DRY_RUN -eq 1 ]]; then
      echo "Dry-run mapping preview: ${mapping_csv}"
    else
      set_step "write driver/agent env configuration"
      if set_env_value_if_changed "$ENV_FILE" "NODE_DRIVER_GRPC_TARGET" "127.0.0.1:50051"; then ENV_CHANGED=1; fi
      if set_env_value_if_changed "$ENV_FILE" "NODE_AGENT_TARGETS" "$mapping_csv"; then ENV_CHANGED=1; fi
      if set_env_value_if_changed "$ENV_FILE" "NODE_AGENT_CA_CERT" "$TLS_CA_CERT"; then ENV_CHANGED=1; fi
      if set_env_value_if_changed "$ENV_FILE" "NODE_AGENT_CLIENT_CERT" "$TLS_CLIENT_CERT"; then ENV_CHANGED=1; fi
      if set_env_value_if_changed "$ENV_FILE" "NODE_AGENT_CLIENT_KEY" "$TLS_CLIENT_KEY"; then ENV_CHANGED=1; fi
      echo "Configured NODE_AGENT_TARGETS in ${ENV_FILE}: ${mapping_csv}"
      # A token enrollment of these nodes is older than this install; the
      # driver must not keep its target over the one written here.
      local enrolled="${TLS_ROOT}/enrolled-agents" installed_mapping
      if [[ -f "$enrolled" ]]; then
        for installed_mapping in "${local_mappings[@]}"; do
          if grep -q "^${installed_mapping%%=*}=" "$enrolled"; then
            grep -v "^${installed_mapping%%=*}=" "$enrolled" > "${enrolled}.new" || true
            mv "${enrolled}.new" "$enrolled"
            ENV_CHANGED=1
          fi
        done
      fi
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
    sudo systemctl restart node-plane-driver
    sudo systemctl is-active --quiet node-plane-driver
  else
    echo "No local driver/env changes detected; skipping final node-plane-driver restart."
  fi
fi

if [[ -n "$BACKEND_NODE_KEY" && $DRY_RUN -eq 0 ]]; then
  set_step "verify backend agent route for ${BACKEND_NODE_KEY}"
  PYTHONPATH="${APP_ROOT}/app" NODE_PLANE_APP_DIR="${APP_ROOT}" NODE_PLANE_SHARED_DIR="${SHARED_ROOT}" \
    "$PYTHON_BIN" - "$BACKEND_NODE_KEY" <<'PY'
import sys
from backend.driver_transport import GrpcIntentDriver, local_channel
with local_channel('127.0.0.1:50051') as channel:
    observation = GrpcIntentDriver(channel).inspect_node(sys.argv[1])
print(f"Backend agent route verified: {observation['node_key']} ({observation['health_state']})")
PY
fi

if [[ $DRY_RUN -eq 0 && $STRICT_MODE -eq 1 && $SKIP_DRIVER -eq 0 && $SKIP_AGENTS -eq 0 ]]; then
  release_commit=""
  if [[ -f "${APP_ROOT}/BUILD_COMMIT" ]]; then
    release_commit="$(tr -d '\r\n' < "${APP_ROOT}/BUILD_COMMIT")"
  elif git -C "$APP_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    release_commit="$(git -C "$APP_ROOT" rev-parse HEAD)"
  fi
  if [[ -n "$release_commit" ]]; then
    printf '%s\n' "$release_commit" > "${SHARED_ROOT}/driver-agent-installed-commit"
  fi
fi

echo
if [[ $DRY_RUN -eq 1 ]]; then
  echo "Driver/agent dry-run finished."
else
  echo "Driver/agent setup finished."
  for notice in "${JOURNAL_ARCHIVE_NOTICES[@]}"; do
    echo "$notice"
    IFS='|' read -r _archive_marker archive_node archive_path <<< "$notice"
    echo "Previous installation journals on ${archive_node} were archived to ${archive_path}."
  done
fi
