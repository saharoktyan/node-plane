#!/usr/bin/env bash
set -euo pipefail
source /etc/node-plane/node.env

if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "Usage: $0 <name> <uuid> [short_id]" >&2
  exit 1
fi

# short_id is retained for the agent RPC contract; REALITY shortIds belong to
# the inbound and the client links use the server's existing short ID.
# The agent does the work (protocols/xray/user_sync.rs); this script stays
# only as the name the profile intent journal runs. An agent without
# `xray-user` would start its server instead: the probe settings make it exit.
NODE_AGENT_CONFIG_PATH=/nonexistent/node-plane-agent-probe.toml NODE_AGENT_LISTEN_ADDR=probe \
exec "${NODE_PLANE_AGENT_BIN:-/usr/local/bin/node-plane-agent}" xray-user add \
  "${XRAY_CONFIG:-/opt/node-plane-runtime/xray/config.json}" \
  "${XRAY_CONTAINER_NAME:-xray}" \
  "${XRAY_INBOUND_TCP_TAG:-reality-tcp}" \
  "${XRAY_INBOUND_XHTTP_TAG:-reality-xhttp}" "$1" "$2"
