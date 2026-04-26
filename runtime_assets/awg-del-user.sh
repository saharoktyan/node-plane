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
NAME="${1:-}"
if [[ -z "$NAME" ]]; then
  echo "Usage: $0 <name>" >&2
  exit 1
fi
if ! docker_cmd ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
  echo "Container ${CONTAINER} not found" >&2
  exit 1
fi
tmp="$(mktemp)"
awk -v name="$NAME" '
  {
    if ($0 == "# " name) {skip=1; next}
    if (skip && NF==0) {skip=0; next}
    if (skip) next
    print
  }
' "$CFG" > "$tmp"
cp -a "$CFG" "${CFG}.bak.$(date +%Y%m%d-%H%M%S)"
mv "$tmp" "$CFG"
docker_cmd restart "$CONTAINER" >/dev/null
echo "OK"
