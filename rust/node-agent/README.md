# node-plane-agent

Rust skeleton for the future node-local agent.

Current scope:

- loads node-local config;
- exposes a small gRPC surface;
- reports runtime facts and local health;
- lists AWG/Xray remote profiles from local files;
- checks node-local port availability for requested runtime ports;
- writes `node.env` from central-driver payloads;
- runs a heartbeat loop.

This crate is still a scaffold, but it is now usable as a local read-only
agent for central-driver transport tests.

## Run

```bash
scripts/run_node_agent.sh
```

Optional environment variables:

- `NODE_AGENT_CONFIG_PATH`
- `NODE_AGENT_NODE_KEY`
- `NODE_AGENT_LISTEN_ADDR`
- `NODE_AGENT_HEARTBEAT_SECONDS`
- `NODE_AGENT_CONFIG_PATH`

Default config path:

- `/etc/node-plane/agent.toml`

Current RPC surface:

- `GetRuntimeFacts`
- `GetNodeHealth`
- `ListRemoteProfiles`
- `RunDiagnostics`
- `CheckPorts`
- `SyncNodeEnv`
