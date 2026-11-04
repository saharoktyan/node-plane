#!/usr/bin/env python3
"""Lease units of earlier releases ran this script; the agent rewrites them
when it starts. Until then it starts the agent's lease commands."""
import os
import sys

AGENT = '/usr/local/bin/node-plane-agent'
COMMANDS = {'monitor-leases': ['lease', 'monitor'], 'start-leased-runtime': ['lease', 'supervise']}

if __name__ == '__main__':
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        raise SystemExit('usage: apply-profile-intent.py monitor-leases | start-leased-runtime PROTOCOL')
    os.execve(AGENT, [AGENT, *COMMANDS[sys.argv[1]], *sys.argv[2:]], dict(os.environ,
        NODE_AGENT_CONFIG_PATH='/nonexistent/node-plane-agent-probe.toml', NODE_AGENT_LISTEN_ADDR='probe'))
