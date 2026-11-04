#!/usr/bin/env bash
set -euo pipefail

# Usage: init-xray.sh CONFIG PUBLIC_HOST [SNI TCP_PORT XHTTP_PORT PATH FLOW
# IMAGE]. The agent does the work (protocols/xray/setup.rs); this script
# stays only as the name the profile intent journal runs. An agent without
# `xray-config` would start its server instead: the probe settings make it
# exit.
NODE_AGENT_CONFIG_PATH=/nonexistent/node-plane-agent-probe.toml NODE_AGENT_LISTEN_ADDR=probe \
exec "${NODE_PLANE_AGENT_BIN:-/usr/local/bin/node-plane-agent}" xray-config init "$@"
