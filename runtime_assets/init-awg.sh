#!/usr/bin/env bash
set -euo pipefail
set -a
source /etc/node-plane/node.env
set +a

# The agent does the work (protocols/awg/setup.rs); this script stays only
# as the name the profile intent journal runs. An agent without
# `awg-config` would start its server instead: the probe settings make it
# exit.
NODE_AGENT_CONFIG_PATH=/nonexistent/node-plane-agent-probe.toml NODE_AGENT_LISTEN_ADDR=probe \
exec "${NODE_PLANE_AGENT_BIN:-/usr/local/bin/node-plane-agent}" awg-config init
