#!/usr/bin/env bash

run_as_root() {
  if [[ "$(id -u)" == "0" ]]; then
    "$@"
    return $?
  fi
  if command -v sudo >/dev/null 2>&1; then
    sudo -n "$@"
    return $?
  fi
  echo "Root privileges are required to run: $*" >&2
  return 1
}

random_alnum() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 18
    return 0
  fi
  tr -dc 'A-Za-z0-9' </dev/urandom | head -c 36
}

env_set() {
  local file="$1"
  local key="$2"
  local value="$3"
  if declare -F set_env_value_in_file >/dev/null 2>&1; then
    set_env_value_in_file "$file" "$key" "$value"
    return 0
  fi
  if [[ -z "$file" ]]; then
    echo "env_set requires a target env file path" >&2
    return 1
  fi
  mkdir -p "$(dirname "$file")"
  touch "$file"
  if grep -q "^${key}=" "$file"; then
    sed -i "s|^${key}=.*$|${key}=${value}|" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
  return 0
}

install_packages_if_needed() {
  if command -v apt-get >/dev/null 2>&1; then
    run_as_root apt-get update || {
      echo "Failed to refresh apt package metadata." >&2
      return 1
    }
    run_as_root apt-get install -y "$@" || {
      echo "Failed to install packages via apt: $*" >&2
      return 1
    }
    return 0
  fi
  if command -v dnf >/dev/null 2>&1; then
    run_as_root dnf install -y "$@" || {
      echo "Failed to install packages via dnf: $*" >&2
      return 1
    }
    return 0
  fi
  if command -v yum >/dev/null 2>&1; then
    run_as_root yum install -y "$@" || {
      echo "Failed to install packages via yum: $*" >&2
      return 1
    }
    return 0
  fi
  if command -v apk >/dev/null 2>&1; then
    run_as_root apk add --no-cache "$@" || {
      echo "Failed to install packages via apk: $*" >&2
      return 1
    }
    return 0
  fi
  echo "No supported package manager found to install: $*" >&2
  return 1
}

ensure_download_tool() {
  if command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1; then
    return 0
  fi
  install_packages_if_needed curl ca-certificates
}

docker_is_usable() {
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    return 0
  fi
  if command -v docker >/dev/null 2>&1 && command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

docker_run() {
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    docker "$@"
    return $?
  fi
  run_as_root docker "$@"
}

start_docker_service_if_needed() {
  if docker_is_usable; then
    return 0
  fi
  if command -v systemctl >/dev/null 2>&1; then
    run_as_root systemctl enable --now docker || true
  elif command -v service >/dev/null 2>&1; then
    run_as_root service docker start || true
  fi
  if ! docker_is_usable; then
    echo "Docker is installed but the daemon is still unavailable for the current user." >&2
    return 1
  fi
}

install_docker_if_missing() {
  if docker_is_usable; then
    return 0
  fi

  if ! command -v docker >/dev/null 2>&1; then
    echo "Docker is not installed; attempting automatic installation..." >&2
    ensure_download_tool || {
      echo "Unable to install download tools required for Docker bootstrap." >&2
      return 1
    }
    local installer
    installer="$(mktemp)"
    if command -v curl >/dev/null 2>&1; then
      curl -fsSL https://get.docker.com -o "$installer" || {
        rm -f "$installer"
        echo "Failed to download Docker installer via curl." >&2
        return 1
      }
    else
      wget -qO "$installer" https://get.docker.com || {
        rm -f "$installer"
        echo "Failed to download Docker installer via wget." >&2
        return 1
      }
    fi
    chmod +x "$installer"
    run_as_root sh "$installer" || {
      rm -f "$installer"
      echo "Automatic Docker installation failed while running the installer script." >&2
      return 1
    }
    rm -f "$installer"
  fi

  start_docker_service_if_needed
}

docker_compose_is_usable() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    return 0
  fi
  if command -v docker-compose >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

install_docker_compose_if_missing() {
  if docker_compose_is_usable; then
    return 0
  fi

  echo "Docker Compose is not available; attempting automatic installation..." >&2
  if command -v apt-get >/dev/null 2>&1; then
    install_packages_if_needed docker-compose-plugin || true
  elif command -v dnf >/dev/null 2>&1; then
    install_packages_if_needed docker-compose-plugin || true
  elif command -v yum >/dev/null 2>&1; then
    install_packages_if_needed docker-compose-plugin || true
  elif command -v apk >/dev/null 2>&1; then
    install_packages_if_needed docker-cli-compose || true
  fi

  if docker_compose_is_usable; then
    return 0
  fi

  ensure_download_tool || {
    echo "Unable to install download tools required for Docker Compose bootstrap." >&2
    return 1
  }

  local arch target
  case "$(uname -m)" in
    x86_64|amd64) arch="x86_64" ;;
    aarch64|arm64) arch="aarch64" ;;
    *)
      echo "Unsupported architecture for automatic Docker Compose install: $(uname -m)" >&2
      return 1
      ;;
  esac
  target="/usr/local/bin/docker-compose"
  if command -v curl >/dev/null 2>&1; then
    run_as_root curl -fsSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-${arch}" -o "$target" || {
      echo "Failed to download Docker Compose binary." >&2
      return 1
    }
  else
    run_as_root wget -qO "$target" "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-${arch}" || {
      echo "Failed to download Docker Compose binary." >&2
      return 1
    }
  fi
  run_as_root chmod +x "$target" || {
    echo "Failed to mark Docker Compose binary as executable." >&2
    return 1
  }

  if ! docker_compose_is_usable; then
    echo "Docker Compose installation completed but the command is still unavailable." >&2
    return 1
  fi
}

load_postgres_runtime_config() {
  local runtime_env_file="$1"
  local shared_dir="$2"

  POSTGRES_RUNTIME_DB_BACKEND="$(read_env_value DB_BACKEND "$runtime_env_file")"
  POSTGRES_RUNTIME_DSN="$(read_env_value POSTGRES_DSN "$runtime_env_file")"
  POSTGRES_RUNTIME_SQLITE_PATH="$(read_env_value SQLITE_DB_PATH "$runtime_env_file")"
  POSTGRES_RUNTIME_DB_NAME="$(read_env_value NODE_PLANE_POSTGRES_DB "$runtime_env_file")"
  POSTGRES_RUNTIME_DB_USER="$(read_env_value NODE_PLANE_POSTGRES_USER "$runtime_env_file")"
  POSTGRES_RUNTIME_DB_PASSWORD="$(read_env_value NODE_PLANE_POSTGRES_PASSWORD "$runtime_env_file")"
  POSTGRES_RUNTIME_PORT="$(read_env_value NODE_PLANE_POSTGRES_PORT "$runtime_env_file")"
  POSTGRES_RUNTIME_CONTAINER="$(read_env_value NODE_PLANE_POSTGRES_CONTAINER "$runtime_env_file")"
  POSTGRES_RUNTIME_IMAGE="$(read_env_value NODE_PLANE_POSTGRES_IMAGE "$runtime_env_file")"

  if [[ -z "$POSTGRES_RUNTIME_DB_BACKEND" || "$POSTGRES_RUNTIME_DB_BACKEND" == "sqlite" ]]; then
    POSTGRES_RUNTIME_DB_BACKEND="postgres"
  fi
  if [[ -z "$POSTGRES_RUNTIME_SQLITE_PATH" ]]; then
    POSTGRES_RUNTIME_SQLITE_PATH="${shared_dir}/data/bot.sqlite3"
  fi
  if [[ -z "$POSTGRES_RUNTIME_DB_NAME" ]]; then
    POSTGRES_RUNTIME_DB_NAME="node_plane"
  fi
  if [[ -z "$POSTGRES_RUNTIME_DB_USER" ]]; then
    POSTGRES_RUNTIME_DB_USER="node_plane"
  fi
  if [[ -z "$POSTGRES_RUNTIME_PORT" ]]; then
    POSTGRES_RUNTIME_PORT="55432"
  fi
  if [[ -z "$POSTGRES_RUNTIME_CONTAINER" ]]; then
    POSTGRES_RUNTIME_CONTAINER="node-plane-postgres"
  fi
  if [[ -z "$POSTGRES_RUNTIME_IMAGE" ]]; then
    POSTGRES_RUNTIME_IMAGE="postgres:16-alpine"
  fi
}

dsn_uses_managed_local_postgres() {
  local dsn="$1"
  [[ -n "$dsn" ]] || return 1
  [[ "$dsn" == *"@127.0.0.1:${POSTGRES_RUNTIME_PORT}/${POSTGRES_RUNTIME_DB_NAME}"* ]]
}

normalize_portable_sqlite_source_path() {
  local runtime_env_file="$1"
  local sqlite_path
  sqlite_path="$(read_env_value SQLITE_DB_PATH "$runtime_env_file")"
  if [[ -z "$sqlite_path" ]]; then
    env_set "$runtime_env_file" "SQLITE_DB_PATH" "/opt/node-plane/data/bot.sqlite3"
    return 0
  fi
  case "$sqlite_path" in
    /opt/node-plane/shared/data/*)
      env_set "$runtime_env_file" "SQLITE_DB_PATH" "/opt/node-plane/data/${sqlite_path#/opt/node-plane/shared/data/}"
      ;;
  esac
}

ensure_portable_postgres_env() {
  local runtime_env_file="$1"
  load_postgres_runtime_config "$runtime_env_file" "$PWD"

  if [[ -z "$POSTGRES_RUNTIME_DB_PASSWORD" ]]; then
    POSTGRES_RUNTIME_DB_PASSWORD="$(random_alnum)"
    env_set "$runtime_env_file" "NODE_PLANE_POSTGRES_PASSWORD" "$POSTGRES_RUNTIME_DB_PASSWORD"
  fi
  if [[ -z "$POSTGRES_RUNTIME_DSN" ]]; then
    POSTGRES_RUNTIME_DSN="postgresql://${POSTGRES_RUNTIME_DB_USER}:${POSTGRES_RUNTIME_DB_PASSWORD}@postgres:5432/${POSTGRES_RUNTIME_DB_NAME}"
    env_set "$runtime_env_file" "POSTGRES_DSN" "$POSTGRES_RUNTIME_DSN"
  fi
  env_set "$runtime_env_file" "DB_BACKEND" "postgres"
  env_set "$runtime_env_file" "NODE_PLANE_POSTGRES_DB" "$POSTGRES_RUNTIME_DB_NAME"
  env_set "$runtime_env_file" "NODE_PLANE_POSTGRES_USER" "$POSTGRES_RUNTIME_DB_USER"
  env_set "$runtime_env_file" "NODE_PLANE_POSTGRES_PORT" "5432"
  env_set "$runtime_env_file" "NODE_PLANE_POSTGRES_IMAGE" "$POSTGRES_RUNTIME_IMAGE"
  normalize_portable_sqlite_source_path "$runtime_env_file"
}

auto_provision_simple_postgres() {
  local runtime_env_file="$1"
  local shared_dir="$2"
  local require_docker="${3:-0}"
  local existing_dsn=""
  local managed_local_dsn=0

  load_postgres_runtime_config "$runtime_env_file" "$shared_dir"
  existing_dsn="$POSTGRES_RUNTIME_DSN"
  if [[ -n "$existing_dsn" ]]; then
    if dsn_uses_managed_local_postgres "$existing_dsn"; then
      managed_local_dsn=1
    else
      return 0
    fi
  fi

  if ! docker_is_usable; then
    if [[ "$require_docker" == "1" ]]; then
      install_docker_if_missing || {
        echo "POSTGRES_DSN is not configured and automatic Docker provisioning failed." >&2
        return 1
      }
    else
      return 0
    fi
  fi

  local data_dir="${shared_dir}/postgres"
  if [[ -z "$POSTGRES_RUNTIME_DB_PASSWORD" ]]; then
    POSTGRES_RUNTIME_DB_PASSWORD="$(random_alnum)"
    env_set "$runtime_env_file" "NODE_PLANE_POSTGRES_PASSWORD" "$POSTGRES_RUNTIME_DB_PASSWORD"
  fi

  mkdir -p "$data_dir"
  env_set "$runtime_env_file" "DB_BACKEND" "postgres"
  env_set "$runtime_env_file" "NODE_PLANE_POSTGRES_DB" "$POSTGRES_RUNTIME_DB_NAME"
  env_set "$runtime_env_file" "NODE_PLANE_POSTGRES_USER" "$POSTGRES_RUNTIME_DB_USER"
  env_set "$runtime_env_file" "NODE_PLANE_POSTGRES_PORT" "$POSTGRES_RUNTIME_PORT"
  env_set "$runtime_env_file" "NODE_PLANE_POSTGRES_CONTAINER" "$POSTGRES_RUNTIME_CONTAINER"
  env_set "$runtime_env_file" "NODE_PLANE_POSTGRES_IMAGE" "$POSTGRES_RUNTIME_IMAGE"

  if docker_run inspect "$POSTGRES_RUNTIME_CONTAINER" >/dev/null 2>&1; then
    local container_env container_password container_user container_db
    container_env="$(docker_run inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$POSTGRES_RUNTIME_CONTAINER" 2>/dev/null || true)"
    container_password="$(printf '%s\n' "$container_env" | awk -F= '/^POSTGRES_PASSWORD=/{print substr($0, index($0,$2)); exit}')"
    container_user="$(printf '%s\n' "$container_env" | awk -F= '/^POSTGRES_USER=/{print substr($0, index($0,$2)); exit}')"
    container_db="$(printf '%s\n' "$container_env" | awk -F= '/^POSTGRES_DB=/{print substr($0, index($0,$2)); exit}')"
    if [[ -n "$container_password" ]]; then
      POSTGRES_RUNTIME_DB_PASSWORD="$container_password"
      env_set "$runtime_env_file" "NODE_PLANE_POSTGRES_PASSWORD" "$POSTGRES_RUNTIME_DB_PASSWORD"
    fi
    if [[ -n "$container_user" ]]; then
      POSTGRES_RUNTIME_DB_USER="$container_user"
      env_set "$runtime_env_file" "NODE_PLANE_POSTGRES_USER" "$POSTGRES_RUNTIME_DB_USER"
    fi
    if [[ -n "$container_db" ]]; then
      POSTGRES_RUNTIME_DB_NAME="$container_db"
      env_set "$runtime_env_file" "NODE_PLANE_POSTGRES_DB" "$POSTGRES_RUNTIME_DB_NAME"
    fi
    docker_run start "$POSTGRES_RUNTIME_CONTAINER" >/dev/null 2>&1 || {
      echo "Failed to start existing PostgreSQL container ${POSTGRES_RUNTIME_CONTAINER}." >&2
      return 1
    }
  else
    docker_run run -d \
      --name "$POSTGRES_RUNTIME_CONTAINER" \
      --restart unless-stopped \
      -e "POSTGRES_DB=${POSTGRES_RUNTIME_DB_NAME}" \
      -e "POSTGRES_USER=${POSTGRES_RUNTIME_DB_USER}" \
      -e "POSTGRES_PASSWORD=${POSTGRES_RUNTIME_DB_PASSWORD}" \
      -p "127.0.0.1:${POSTGRES_RUNTIME_PORT}:5432" \
      -v "${data_dir}:/var/lib/postgresql/data" \
      "$POSTGRES_RUNTIME_IMAGE" >/dev/null || {
      echo "Failed to create PostgreSQL container ${POSTGRES_RUNTIME_CONTAINER} from image ${POSTGRES_RUNTIME_IMAGE}." >&2
      return 1
    }
  fi

  # Keep DB role password aligned with runtime config to avoid endless restarts
  # when .env password and in-DB password drift after failed/retried installs.
  local escaped_role_password
  escaped_role_password="${POSTGRES_RUNTIME_DB_PASSWORD//\'/\'\'}"
  if docker_run exec \
    -e PGPASSWORD="$POSTGRES_RUNTIME_DB_PASSWORD" \
    "$POSTGRES_RUNTIME_CONTAINER" \
    psql -h 127.0.0.1 -U "$POSTGRES_RUNTIME_DB_USER" -d "$POSTGRES_RUNTIME_DB_NAME" -c "SELECT 1;" >/dev/null 2>&1; then
    :
  else
    docker_run exec \
      -e PGPASSWORD="$POSTGRES_RUNTIME_DB_PASSWORD" \
      "$POSTGRES_RUNTIME_CONTAINER" \
      psql -h 127.0.0.1 -U postgres -d postgres \
      -c "ALTER ROLE ${POSTGRES_RUNTIME_DB_USER} WITH PASSWORD '${escaped_role_password}';" >/dev/null 2>&1 || true
  fi

  local ready=0
  for _ in $(seq 1 30); do
    if docker_run exec "$POSTGRES_RUNTIME_CONTAINER" pg_isready -U "$POSTGRES_RUNTIME_DB_USER" -d "$POSTGRES_RUNTIME_DB_NAME" >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 1
  done
  if [[ "$ready" != "1" ]]; then
    echo "Automatic PostgreSQL provisioning failed: container ${POSTGRES_RUNTIME_CONTAINER} did not become ready." >&2
    docker_run logs --tail 50 "$POSTGRES_RUNTIME_CONTAINER" >&2 || true
    return 1
  fi

  POSTGRES_RUNTIME_DSN="postgresql://${POSTGRES_RUNTIME_DB_USER}:${POSTGRES_RUNTIME_DB_PASSWORD}@127.0.0.1:${POSTGRES_RUNTIME_PORT}/${POSTGRES_RUNTIME_DB_NAME}"
  env_set "$runtime_env_file" "POSTGRES_DSN" "$POSTGRES_RUNTIME_DSN"
  if [[ "$managed_local_dsn" == "1" ]]; then
    echo "Verified local PostgreSQL runtime: ${POSTGRES_RUNTIME_CONTAINER}"
  else
    echo "Auto-provisioned local PostgreSQL runtime: ${POSTGRES_RUNTIME_CONTAINER}"
    echo "PostgreSQL DSN persisted to ${runtime_env_file}"
  fi
}
