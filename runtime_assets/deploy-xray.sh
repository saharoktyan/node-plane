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
IMAGE="${XRAY_DOCKER_IMAGE:-ghcr.io/xtls/xray-core:25.12.8}"
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

if docker_cmd ps -a --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
  docker_cmd rm -f "$CONTAINER" >/dev/null
fi

docker_cmd pull "$IMAGE" >/dev/null
docker_cmd run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  --user 0:0 \
  --network host \
  -v "$CONFIG:/etc/xray/config.json:ro" \
  "$IMAGE" run -c /etc/xray/config.json >/dev/null

echo "Xray container deployed: $CONTAINER"
