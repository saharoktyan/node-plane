#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

MODE="${MODE:-auto}"
SKIP_PULL=0
SKIP_DEPS=0
SKIP_RESTART=0
HEALTH_TIMEOUT=30

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
    --skip-pull)
      SKIP_PULL=1
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
  scripts/update.sh [--mode auto|simple|portable] [--skip-pull] [--skip-deps] [--skip-restart] [--health-timeout 30]

Modes:
  auto      Detect update mode from local environment
  simple    Update the host/systemd deployment with rollback support
  portable  Update the Docker Compose deployment

Flags:
  --skip-pull        Do not run git pull --ff-only
  --skip-deps        Skip dependency reinstall in portable mode. Not supported in simple mode.
  --skip-restart     Do not restart the service/container after applying changes
  --health-timeout   Seconds to wait for node-plane.service to become active after restart
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

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

read_env_value() {
  local key="$1"
  local file="${2:-.env}"
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

  if has_cmd systemctl && systemctl list-unit-files node-plane.service >/dev/null 2>&1; then
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
  if git rev-parse --short HEAD >/dev/null 2>&1; then
    git rev-parse --short HEAD
  else
    echo "unknown"
  fi
}

release_id_base() {
  local semver commit
  semver="$(read_version)"
  commit="$(current_git_commit)"
  if [[ "$commit" == "unknown" ]]; then
    echo "${semver}"
  else
    echo "${semver}-${commit}"
  fi
}

unique_release_id() {
  local releases_dir="$1"
  local base candidate suffix
  base="$(release_id_base)"
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
  set_env_value_in_file ".env" "NODE_PLANE_SOURCE_DIR" "$REPO_ROOT"
  set_env_value_in_file ".env" "NODE_PLANE_INSTALL_MODE" "$MODE"
  cp .env "${shared_dir}/.env"
}

pull_code() {
  if [[ $SKIP_PULL -eq 1 ]]; then
    echo "Skipping git pull"
    return 0
  fi
  need_cmd git
  echo "Updating git checkout..."
  git pull --ff-only
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
  while (( elapsed < timeout )); do
    if sudo systemctl is-active --quiet node-plane.service; then
      return 0
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done
  return 1
}

portable_compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
    return 0
  fi
  if has_cmd docker-compose; then
    docker-compose "$@"
    return 0
  fi
  echo "Docker Compose is required for portable mode." >&2
  exit 1
}

wait_for_container() {
  local timeout="$1"
  local elapsed=0
  while (( elapsed < timeout )); do
    local status restart_count
    status="$(docker inspect -f '{{.State.Status}}' node-plane 2>/dev/null || true)"
    restart_count="$(docker inspect -f '{{.RestartCount}}' node-plane 2>/dev/null || echo "0")"
    if [[ "$status" == "running" && "$restart_count" == "0" ]]; then
      return 0
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done
  return 1
}

rollback_portable() {
  local previous_tag="$1"
  local image_repo="$2"

  if [[ -z "$previous_tag" ]]; then
    echo "No previous portable image tag is available for rollback." >&2
    docker ps -a --filter "name=node-plane" || true
    docker logs --tail 100 node-plane || true
    exit 1
  fi

  echo "Rolling back portable deployment to ${image_repo}:${previous_tag} ..."
  set_env_value_in_file ".env" "NODE_PLANE_IMAGE_REPO" "$image_repo"
  set_env_value_in_file ".env" "NODE_PLANE_IMAGE_TAG" "$previous_tag"
  portable_compose up -d
  if wait_for_container "$HEALTH_TIMEOUT"; then
    echo "Portable rollback completed."
    exit 1
  fi

  echo "Portable rollback failed. Inspect the container manually." >&2
  docker ps -a --filter "name=node-plane" || true
  docker logs --tail 100 node-plane || true
  exit 1
}

rollback_simple() {
  local previous_release="$1"
  local current_link="$2"
  local failed_release="$3"

  if [[ -z "$previous_release" || ! -d "$previous_release" ]]; then
    echo "No previous release is available for rollback." >&2
    echo "Failed release remains at: ${failed_release}" >&2
    sudo systemctl status node-plane --no-pager || true
    sudo journalctl -u node-plane -n 50 --no-pager || true
    exit 1
  fi

  echo "Rolling back to previous release:"
  echo "  ${previous_release}"
  ln -sfn "$previous_release" "$current_link"
  sudo systemctl daemon-reload
  sudo systemctl restart node-plane
  if wait_for_service "$HEALTH_TIMEOUT"; then
    echo "Rollback completed."
    echo "Failed release remains at: ${failed_release}"
    exit 1
  fi

  echo "Rollback failed. Inspect the service manually." >&2
  sudo systemctl status node-plane --no-pager || true
  sudo journalctl -u node-plane -n 80 --no-pager || true
  exit 1
}

update_simple() {
  need_cmd python3
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

  local previous_release new_release_name new_release_dir
  previous_release="$(readlink -f "$current_link" 2>/dev/null || true)"
  new_release_name="$(unique_release_id "$releases_dir")"
  new_release_dir="${releases_dir}/${new_release_name}"

  mkdir -p "$releases_dir" "${shared_dir}/data" "${shared_dir}/ssh"
  sync_shared_env "$shared_dir"

  echo "Preparing new release:"
  echo "  ${new_release_dir}"
  export_release_tree "$new_release_dir"

  echo "Installing Python runtime for new release..."
  python3 -m venv "${new_release_dir}/.venv"
  "${new_release_dir}/.venv/bin/python" -m pip install --upgrade pip setuptools wheel
  "${new_release_dir}/.venv/bin/python" -m pip install -r "${new_release_dir}/requirements.txt"

  echo "Applying database/schema init..."
  NODE_PLANE_BASE_DIR="${base_dir}" \
  NODE_PLANE_APP_DIR="${new_release_dir}" \
  NODE_PLANE_SHARED_DIR="${shared_dir}" \
  "${new_release_dir}/.venv/bin/python" "${new_release_dir}/app/manage_db.py" init

  if [[ $SKIP_RESTART -eq 1 ]]; then
    echo "Skipping service restart"
    echo "Release is prepared but not activated:"
    echo "  ${new_release_dir}"
    exit 0
  fi

  echo "Switching current release..."
  ln -sfn "$new_release_dir" "$current_link"

  echo "Restarting node-plane.service..."
  sudo systemctl daemon-reload
  if ! sudo systemctl restart node-plane; then
    echo "Service restart failed immediately."
    rollback_simple "$previous_release" "$current_link" "$new_release_dir"
  fi

  if wait_for_service "$HEALTH_TIMEOUT"; then
    echo "New release is healthy."
    sudo systemctl status node-plane --no-pager || true
    return 0
  fi

  echo "Updated release did not become healthy within ${HEALTH_TIMEOUT}s."
  sudo journalctl -u node-plane -n 50 --no-pager || true
  rollback_simple "$previous_release" "$current_link" "$new_release_dir"
}

update_portable() {
  need_cmd docker

  local image_repo image_tag previous_tag new_tag
  image_repo="$(read_env_value NODE_PLANE_IMAGE_REPO)"
  image_tag="$(read_env_value NODE_PLANE_IMAGE_TAG)"
  previous_tag="${image_tag:-local}"

  if [[ -z "$image_repo" ]]; then
    image_repo="node-plane"
  fi
  if [[ -z "$image_tag" ]]; then
    image_tag="local"
  fi

  if [[ "$image_repo" == "node-plane" && "$image_tag" == "local" ]]; then
    if [[ $SKIP_RESTART -eq 0 ]]; then
      echo "Rebuilding and restarting local Docker Compose deployment..."
      portable_compose up -d --build
      if wait_for_container "$HEALTH_TIMEOUT"; then
        echo "Portable local-build deployment is healthy."
        return 0
      fi
      echo "Portable local-build deployment did not become healthy within ${HEALTH_TIMEOUT}s." >&2
      docker logs --tail 100 node-plane || true
      exit 1
    fi
    echo "Skipping Docker Compose restart"
    return 0
  fi

  new_tag="$(release_id_base)"
  echo "Portable registry update target: ${image_repo}:${new_tag}"
  set_env_value_in_file ".env" "NODE_PLANE_PREVIOUS_IMAGE_TAG" "$previous_tag"
  set_env_value_in_file ".env" "NODE_PLANE_IMAGE_REPO" "$image_repo"
  set_env_value_in_file ".env" "NODE_PLANE_IMAGE_TAG" "$new_tag"

  if ! docker pull "${image_repo}:${new_tag}"; then
    echo "Failed to pull ${image_repo}:${new_tag}" >&2
    set_env_value_in_file ".env" "NODE_PLANE_IMAGE_TAG" "$previous_tag"
    exit 1
  fi

  if [[ $SKIP_RESTART -ne 0 ]]; then
    echo "Skipping Docker Compose restart"
    return 0
  fi

  echo "Restarting portable Docker Compose deployment..."
  portable_compose up -d
  if wait_for_container "$HEALTH_TIMEOUT"; then
    echo "Portable registry deployment is healthy."
    return 0
  fi

  echo "Portable registry deployment did not become healthy within ${HEALTH_TIMEOUT}s." >&2
  docker logs --tail 100 node-plane || true
  rollback_portable "$previous_tag" "$image_repo"
}

main() {
  detect_mode
  echo "Detected update mode: ${MODE}"
  echo "Current checkout version: $(print_version)"
  pull_code
  echo "Updated checkout version: $(print_version)"

  case "$MODE" in
    simple)
      update_simple
      ;;
    portable)
      update_portable
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
