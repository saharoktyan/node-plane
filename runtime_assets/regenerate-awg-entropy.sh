#!/usr/bin/env bash
set -euo pipefail
source /etc/node-plane/node.env

CFG="${AWG_CONFIG:-/opt/node-plane-runtime/amnezia-awg/data/wg0.conf}"
PRESET="${AWG_I1_PRESET:-quic}"
CONTAINER="${AWG_CONTAINER_NAME:-amnezia-awg}"
PROFILE_TOOL="${AWG_PROFILE_TOOL:-/opt/node-plane-runtime/awg_profile.py}"

[[ -f "$CFG" ]] || { echo "AWG config not found: $CFG" >&2; exit 1; }
BACKUP="$(mktemp "$(dirname "$CFG")/.awg-regenerate-backup-XXXXXX")"
cp -p "$CFG" "$BACKUP"
restore_config() {
  cp -p "$BACKUP" "$CFG"
  if docker info >/dev/null 2>&1; then
    docker restart "$CONTAINER" >/dev/null 2>&1 || true
  else
    sudo docker restart "$CONTAINER" >/dev/null 2>&1 || true
  fi
  rm -f "$BACKUP"
  echo "AWG entropy update failed; previous config restored" >&2
}
trap restore_config EXIT
python3 "$PROFILE_TOOL" regenerate "$CFG" "$PRESET"

if docker info >/dev/null 2>&1; then
  docker restart "$CONTAINER" >/dev/null
else
  sudo docker restart "$CONTAINER" >/dev/null
fi
sleep 2
if docker info >/dev/null 2>&1; then
  STATE="$(docker inspect -f '{{.State.Status}}' "$CONTAINER")"
else
  STATE="$(sudo docker inspect -f '{{.State.Status}}' "$CONTAINER")"
fi
[[ "$STATE" == "running" ]] || { echo "AWG container is $STATE after entropy update" >&2; exit 1; }
trap - EXIT
rm -f "$BACKUP"
/opt/node-plane-runtime/show-awg-entropy.sh
echo "WARNING: client AWG configs must be reissued after entropy regeneration."
