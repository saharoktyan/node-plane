#!/usr/bin/env bash
set -euo pipefail
set -a
source /etc/node-plane/node.env
set +a

# Usage: apply-node-settings.sh XRAY_CONFIG SNI TCP_PORT XHTTP_PORT PATH
# APPLY_XRAY APPLY_AWG. The agent does the work (protocols/settings.rs);
# this script stays only as the name the profile intent journal runs. An
# agent without `node-settings` would start its server instead: the probe
# settings make it exit.
NODE_AGENT_CONFIG_PATH=/nonexistent/node-plane-agent-probe.toml NODE_AGENT_LISTEN_ADDR=probe \
exec "${NODE_PLANE_AGENT_BIN:-/usr/local/bin/node-plane-agent}" node-settings apply \
  "${1:-}" "${2:-}" "${3:-}" "${4:-}" "${5:-/assets}" "${6:-false}" "${7:-false}"
