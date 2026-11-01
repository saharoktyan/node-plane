#!/usr/bin/env bash
set -euo pipefail
source /etc/node-plane/node.env

docker_cmd() {
  if docker info >/dev/null 2>&1; then
    docker "$@"
    return $?
  fi
  if command -v sudo >/dev/null 2>&1; then
    sudo systemctl enable --now docker >/dev/null 2>&1 || sudo service docker start >/dev/null 2>&1 || true
    if sudo docker info >/dev/null 2>&1; then
      sudo docker "$@"
      return $?
    fi
  fi
  systemctl enable --now docker >/dev/null 2>&1 || service docker start >/dev/null 2>&1 || true
  if docker info >/dev/null 2>&1; then
    docker "$@"
    return $?
  fi
  echo "Docker daemon is unavailable for this session." >&2
  id >&2 || true
  groups >&2 || true
  ls -l /var/run/docker.sock >&2 || true
  exit 1
}

DOCKER_DIR="${XRAY_DOCKER_DIR:-/opt/node-plane-runtime/xray}"
IMAGE="${XRAY_DOCKER_IMAGE:-ghcr.io/xtls/xray-core:26.10.29@sha256:c9443f3ee297f6481bc245269d61720c6f989195ee9b58fa90fdd99f03c21044}"
CONTAINER="${XRAY_CONTAINER_NAME:-xray}"
CONFIG="${XRAY_CONFIG:-/opt/node-plane-runtime/xray/config.json}"

prepare_leased() {
  if [[ -f /etc/systemd/system/node-plane-leased-xray.service ]]; then
    python3 /opt/node-plane-runtime/apply-profile-intent.py prepare-leased-runtime xray >/dev/null
  fi
}

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
source "$(dirname "${BASH_SOURCE[0]}")/deploy-container-guard.sh"
container_guard_init xray "$CONTAINER" "$(dirname "$CONFIG")" /etc/xray
trap 'rm -rf -- "$NP_GUARD_DIR"' EXIT
PREVIOUS_ID="$(container_guard_existing)"
prepare_leased

docker_cmd pull "$IMAGE" >/dev/null
docker_cmd run --rm \
  --user 0:0 \
  -v "$(dirname "$CONFIG"):/etc/xray:ro" \
  "$IMAGE" run -test -c /etc/xray/config.json >/dev/null

PREVIOUS_CONTAINER=""
if [[ -n "$PREVIOUS_ID" ]]; then
  PREVIOUS_CONTAINER="${CONTAINER}-previous-$$"
  container_guard_validate "$PREVIOUS_ID" "$CONTAINER" >/dev/null
  docker_cmd rename "$PREVIOUS_ID" "$PREVIOUS_CONTAINER" >/dev/null
  container_guard_validate "$PREVIOUS_ID" "$PREVIOUS_CONTAINER" >/dev/null
  if ! docker_cmd stop "$PREVIOUS_ID" >/dev/null; then
    container_guard_validate "$PREVIOUS_ID" "$PREVIOUS_CONTAINER" >/dev/null
    docker_cmd rename "$PREVIOUS_ID" "$CONTAINER" >/dev/null
    echo "Could not stop the previous Xray container" >&2
    exit 1
  fi
fi

start_container() {
  docker_cmd run -d \
    --name "$CONTAINER" \
    --cidfile "$NP_CIDFILE" \
    "${NP_CONTAINER_LABELS[@]}" \
    --restart no \
    --user 0:0 \
    --network host \
    -v "$(dirname "$CONFIG"):/etc/xray:ro" \
    "$IMAGE" run -c /etc/xray/config.json >/dev/null
}

restore_previous() {
  container_guard_remove_new || return 1
  if [[ -n "$PREVIOUS_CONTAINER" ]]; then
    container_guard_validate "$PREVIOUS_ID" "$PREVIOUS_CONTAINER" >/dev/null || return 1
    docker_cmd rename "$PREVIOUS_ID" "$CONTAINER" >/dev/null || return 1
    prepare_leased || return 1
    container_guard_validate "$PREVIOUS_ID" "$CONTAINER" >/dev/null || return 1
    if ! docker_cmd start "$PREVIOUS_ID" >/dev/null; then
      echo "Previous Xray container could not be restarted" >&2
      return 1
    fi
  fi
}

fail_deployment() {
  local reason="$1"
  if restore_previous; then
    echo "$reason; rollback completed" >&2
  else
    echo "$reason; rollback could not be confirmed, recovery required" >&2
  fi
  exit 1
}

if ! start_container; then
  fail_deployment 'Xray container failed to start'
fi

sleep 2
NEW_ID="$(container_guard_new)"
if [[ -z "$NEW_ID" ]]; then
  echo 'New Xray container identity was not recorded; recovery required' >&2
  exit 1
fi
if [[ "$(docker_cmd inspect -f '{{.State.Status}}' "$NEW_ID" 2>/dev/null || echo unknown)" != "running" ]]; then
  docker_cmd logs "$NEW_ID" >&2 || true
  fail_deployment 'Xray container did not start with the current config'
fi

if [[ -f /etc/systemd/system/node-plane-leased-xray.service ]]; then
  if ! python3 /opt/node-plane-runtime/apply-profile-intent.py leased-deploy xray >/dev/null; then
    fail_deployment 'Xray lease supervisor could not be started'
  fi
else
  container_guard_validate "$NEW_ID" "$CONTAINER" "$NP_DEPLOYMENT" >/dev/null
  docker_cmd update --restart unless-stopped "$NEW_ID" >/dev/null
fi

if [[ -n "$PREVIOUS_CONTAINER" ]]; then
  container_guard_validate "$PREVIOUS_ID" "$PREVIOUS_CONTAINER" >/dev/null
  docker_cmd rm -f "$PREVIOUS_ID" >/dev/null
fi

echo "Xray container deployed: $CONTAINER ($IMAGE)"
