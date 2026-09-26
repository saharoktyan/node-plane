#!/usr/bin/env bash
set -euo pipefail
source /etc/node-plane/node.env

docker_cmd() {
  if docker info >/dev/null 2>&1; then
    docker "$@"
    return
  fi
  if command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
    sudo docker "$@"
    return
  fi
  echo "Docker is not available for this user." >&2
  exit 1
}

CONTAINER="${AWG_CONTAINER_NAME:-amnezia-awg}"
CFG="${AWG_CONFIG:-/opt/node-plane-runtime/amnezia-awg/data/wg0.conf}"
SERVER_KEY="${SERVER_KEY:-}"
NAME="${1:-}"
if [[ -z "$NAME" ]]; then
  echo "Usage: $0 <name>" >&2
  exit 1
fi
DISPLAY_NAME="$NAME"
if [[ -n "$SERVER_KEY" ]]; then
  DISPLAY_NAME="${SERVER_KEY}-${NAME}"
fi
if [[ ! "$DISPLAY_NAME" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "Invalid AWG profile name" >&2
  exit 1
fi
if ! docker_cmd ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
  echo "Container ${CONTAINER} not found" >&2
  exit 1
fi
CLIENTS_DIR="${AWG_CLIENTS_DIR:-/opt/node-plane-runtime/awg-clients}"
IFACE="${AWG_IFACE:-wg0}"
exec 9>"${CFG}.lock"
flock -x 9
python3 "$(dirname "${BASH_SOURCE[0]}")/awg-peer-state.py" revoke "$CFG" "$CLIENTS_DIR" "$DISPLAY_NAME" "$CONTAINER" "$IFACE"
echo "OK"
