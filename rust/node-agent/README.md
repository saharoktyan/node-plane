# node-plane-agent

Rust skeleton for the future node-local agent.

Current scope:

- loads node-local config;
- prints startup metadata;
- runs a heartbeat loop;
- is ready to receive transport and executor modules later.

This crate does not expose RPC yet. It exists to lock down the node-side
responsibility boundary before central-driver transport work begins.

## Run

```bash
scripts/run_node_agent.sh
```

Optional environment variables:

- `NODE_AGENT_CONFIG_PATH`
- `NODE_AGENT_NODE_KEY`
- `NODE_AGENT_LISTEN_ADDR`
- `NODE_AGENT_HEARTBEAT_SECONDS`

Default config path:

- `/etc/node-plane/agent.toml`
