#!/usr/bin/env bash
set -euo pipefail
source /etc/node-plane/node.env

if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "Usage: $0 <name> <uuid> [short_id]" >&2
  exit 1
fi

# short_id is retained for the agent RPC contract; REALITY shortIds belong to
# the inbound and the client links use the server's existing short ID.
exec python3 /opt/node-plane-runtime/xray-user-api.py add \
  "${XRAY_CONFIG:-/opt/node-plane-runtime/xray/config.json}" \
  "${XRAY_CONTAINER_NAME:-xray}" \
  "${XRAY_INBOUND_TCP_TAG:-reality-tcp}" \
  "${XRAY_INBOUND_XHTTP_TAG:-reality-xhttp}" "$1" "$2"
