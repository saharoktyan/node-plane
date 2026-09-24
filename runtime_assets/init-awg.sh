#!/usr/bin/env bash
set -euo pipefail
source /etc/node-plane/node.env

CFG="${AWG_CONFIG:-/opt/node-plane-runtime/amnezia-awg/data/wg0.conf}"
SERVER_ADDR="${AWG_SERVER_ADDRESS:-10.8.1.0/24}"
PORT="${AWG_SERVER_PORT:-51820}"
PRESET="${AWG_I1_PRESET:-quic}"
PROFILE_TOOL="${AWG_PROFILE_TOOL:-/opt/node-plane-runtime/awg_profile.py}"

mkdir -p "$(dirname "$CFG")"
if [[ -s "$CFG" ]]; then
  echo "AWG config already exists: $CFG"
  exit 0
fi

SERVER_PRIV="$(wg genkey)"
TMP="$(mktemp "$(dirname "$CFG")/.awg-init-XXXXXX")"
trap 'rm -f "$TMP"' EXIT
{
  printf '[Interface]\nPrivateKey = %s\nAddress = %s\nListenPort = %s\n' "$SERVER_PRIV" "$SERVER_ADDR" "$PORT"
  python3 "$PROFILE_TOOL" init "$CFG" "$PRESET"
} > "$TMP"
python3 "$PROFILE_TOOL" validate "$TMP"
chmod 600 "$TMP"
mv "$TMP" "$CFG"
echo "AWG 3.1 config initialized: $CFG"
