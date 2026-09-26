#!/usr/bin/env bash
set -euo pipefail
source /etc/node-plane/node.env

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <name>" >&2
  exit 1
fi

exec python3 /opt/node-plane-runtime/xray-user-api.py delete \
  "${XRAY_CONFIG:-/opt/node-plane-runtime/xray/config.json}" \
  "${XRAY_CONTAINER_NAME:-xray}" \
  "${XRAY_INBOUND_TCP_TAG:-reality-tcp}" \
  "${XRAY_INBOUND_XHTTP_TAG:-reality-xhttp}" "$1"
