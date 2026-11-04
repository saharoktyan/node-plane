#!/usr/bin/env bash
set -euo pipefail
set -a
source /etc/node-plane/node.env
set +a

if [[ $# -ne 1 || -z "$1" ]]; then
  echo "Usage: $0 <name>" >&2
  exit 1
fi

# The agent does the work (protocols/awg/peer_sync.rs); this script stays
# only as the name the profile intent journal runs. An agent without
# `awg-peer` would start its server instead: the probe settings make it exit.
NODE_AGENT_CONFIG_PATH=/nonexistent/node-plane-agent-probe.toml NODE_AGENT_LISTEN_ADDR=probe \
exec "${NODE_PLANE_AGENT_BIN:-/usr/local/bin/node-plane-agent}" awg-peer revoke "$1"
