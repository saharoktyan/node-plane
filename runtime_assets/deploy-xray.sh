#!/usr/bin/env bash
set -euo pipefail
source /etc/node-plane/node.env

docker_cmd() {
  if docker info >/dev/null 2>&1; then
    docker "$@"
    return
  fi
  if command -v sudo >/dev/null 2>&1; then
    sudo systemctl enable --now docker >/dev/null 2>&1 || sudo service docker start >/dev/null 2>&1 || true
    if sudo docker info >/dev/null 2>&1; then
      sudo docker "$@"
      return
    fi
  fi
  systemctl enable --now docker >/dev/null 2>&1 || service docker start >/dev/null 2>&1 || true
  if docker info >/dev/null 2>&1; then
    docker "$@"
    return
  fi
  echo "Docker daemon is unavailable for this session." >&2
  id >&2 || true
  groups >&2 || true
  ls -l /var/run/docker.sock >&2 || true
  exit 1
}

DOCKER_DIR="${XRAY_DOCKER_DIR:-/opt/node-plane-runtime/xray}"
IMAGE="${XRAY_DOCKER_IMAGE:-ghcr.io/xtls/xray-core:26.3.27}"
CONTAINER="${XRAY_CONTAINER_NAME:-xray}"
CONFIG="${XRAY_CONFIG:-/opt/node-plane-runtime/xray/config.json}"

mkdir -p "$DOCKER_DIR"
chmod 0755 /opt >/dev/null 2>&1 || true
chmod 0755 /opt/node-plane-runtime >/dev/null 2>&1 || true
chmod 0755 "$DOCKER_DIR" >/dev/null 2>&1 || true

if [[ -d "$CONFIG" ]]; then
  mv "$CONFIG" "${CONFIG}.dirbak.$(date +%Y%m%d-%H%M%S)"
fi

if [[ ! -f "$CONFIG" ]]; then
  echo "Xray config not found: $CONFIG" >&2
  exit 1
fi

chmod 0600 "$CONFIG" >/dev/null 2>&1 || true

docker_cmd pull "$IMAGE" >/dev/null
docker_cmd run --rm \
  --user 0:0 \
  -v "$(dirname "$CONFIG"):/etc/xray:ro" \
  "$IMAGE" run -test -c /etc/xray/config.json >/dev/null

PREVIOUS_CONTAINER=""
if docker_cmd ps -a --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
  PREVIOUS_CONTAINER="${CONTAINER}-previous-$$"
  docker_cmd rename "$CONTAINER" "$PREVIOUS_CONTAINER" >/dev/null
  if ! docker_cmd stop "$PREVIOUS_CONTAINER" >/dev/null; then
    docker_cmd rename "$PREVIOUS_CONTAINER" "$CONTAINER" >/dev/null
    echo "Could not stop the previous Xray container" >&2
    exit 1
  fi
fi

start_container() {
  docker_cmd run -d \
    --name "$CONTAINER" \
    --restart unless-stopped \
    --user 0:0 \
    --network host \
    -v "$(dirname "$CONFIG"):/etc/xray:ro" \
    "$IMAGE" run -c /etc/xray/config.json >/dev/null
}

restore_previous() {
  docker_cmd rm -f "$CONTAINER" >/dev/null 2>&1 || true
  if [[ -n "$PREVIOUS_CONTAINER" ]]; then
    docker_cmd rename "$PREVIOUS_CONTAINER" "$CONTAINER" >/dev/null
    if ! docker_cmd start "$CONTAINER" >/dev/null; then
      echo "Previous Xray container could not be restarted" >&2
      return 1
    fi
  fi
}

if ! start_container; then
  restore_previous || true
  echo "Xray container failed to start; previous container restored" >&2
  exit 1
fi

sleep 2
if [[ "$(docker_cmd inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo unknown)" != "running" ]]; then
  docker_cmd logs "$CONTAINER" >&2 || true
  restore_previous || true
  echo "Xray container did not start with the current config" >&2
  exit 1
fi

if [[ -n "$PREVIOUS_CONTAINER" ]]; then
  docker_cmd rm -f "$PREVIOUS_CONTAINER" >/dev/null
fi

echo "Xray container deployed: $CONTAINER ($IMAGE)"
