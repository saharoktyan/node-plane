# Node agent

Privileged Rust gRPC executor on each managed node. It reports facts and health, validates ports and runtime settings, manages owned protocol resources and applies explicit profile/node intents.

## Run

```sh
scripts/run_node_agent.sh
cargo test --locked --manifest-path rust/node-agent/Cargo.toml
```

Configuration defaults to `/etc/node-plane/agent.toml`. Environment overrides include `NODE_AGENT_CONFIG_PATH`, `NODE_AGENT_NODE_KEY`, `NODE_AGENT_LISTEN_ADDR` and `NODE_AGENT_HEARTBEAT_SECONDS`.

Mutual TLS is required. `tls_certificate_path`, `tls_key_path` and `tls_client_ca_path` identify the server certificate, private key and client CA; rollout installs them under `/etc/node-plane/tls/`. Restrict the listener to the controller. Missing TLS material prevents startup.

## Execution

Profile and node helpers operate under local locks and durable journals. Preserve command identity, intent fingerprints and fences on restart/update. Unknown outcomes require inspection rather than unconditional replay.

The runtime bundle contains the Python/shell protocol adapters. Check actual runtime facts before reporting installation success. Full decommissioning disables scheduling, stops the agent and removes owned resources with independent host verification.

See [Architecture](../../docs/development/architecture.md), [Temporary access](../../docs/development/temporary-access.md) and [Testing](../../docs/development/testing.md). RPC definitions are under [`proto/`](../../proto/).
