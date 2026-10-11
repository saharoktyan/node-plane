# Node driver

Rust gRPC transport between backend-owned execution intents and node agents. The driver owns execution records and observations; it does not read business tables or require PostgreSQL credentials.

## Run

```sh
scripts/run_node_driver.sh
cargo test --manifest-path rust/node-driver/Cargo.toml
```

| Setting | Purpose |
| --- | --- |
| `NODE_DRIVER_LISTEN_ADDR` | Listener; default `127.0.0.1:50051` |
| `NODE_DRIVER_OPERATIONS_PATH` | Durable operation snapshot; default under shared `data/` |
| `NODE_AGENT_TARGETS` | Node key to agent-address mappings |
| `NODE_AGENT_CONFIG_PATH` | Local agent configuration for loopback discovery |
| `NODE_AGENT_CA_CERT` | Agent trust CA |
| `NODE_AGENT_CLIENT_CERT` / `NODE_AGENT_CLIENT_KEY` | Driver mutual-TLS identity |
| `NODE_PLANE_APP_DIR` / `NODE_PLANE_SHARED_DIR` | Active application and shared state |

The driver loads shared environment settings without overriding explicit process values. The runtime bundle is selected through `runtime_assets/manifest.json`.

## Commands and records

Profile/settings RPCs use `x-node-plane-command-id`; node actions carry a request command ID. Identity, method and request fingerprint prevent duplicate execution. Preserve them through recovery.

Operation snapshots can contain credentials and use private file permissions plus an OS lock. Corrupt/unreadable history prevents startup. The journal must persist before dispatch. An interrupted command has an unknown node outcome and must not be automatically replayed.

`WatchOperation` returns the recorded state and closes; live progress comes from backend read models/polling. See [Architecture](../../docs/development/architecture.md) and [`proto/`](../../proto/) for the execution boundary.
