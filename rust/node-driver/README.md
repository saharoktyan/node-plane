# node-plane-driver

Rust gRPC execution service between the backend and node agents. The backend
owns accounts, profiles, grants, settings, traffic policy and operation queues.
Its transport is `app/backend/driver_transport.py`.

## Current backend contract

- NodeService: `InspectBackendNode` and `GetNodeDiagnostics`. Both consult the
  configured agent; neither falls back to legacy PostgreSQL server records.
- ProvisioningService: explicit profile intents and inspection/recovery/
  resolution. The backend supplies runtime identity, protocol, revision and
  required settings.
- RuntimeService: explicit node actions, settings application/recovery/
  resolution, agent preparation, public Xray metadata, AWG configuration refresh
  and phased decommissioning.
- OperationService: durable command lookup and recorded operation results.
  `WatchOperation` emits the recorded state and closes; it is not live progress.

The unused TelemetryService and old NodeService registry, environment, probe,
port, Docker-install and health-stream RPCs have been removed. Traffic reads use
explicit `BackendNodeAction` intents; aggregation belongs to the backend.
Agent RPCs for host actions remain available internally to the execution layer.

Legacy ProvisioningService and RuntimeService methods, shared registry/profile
helpers and the PostgreSQL client dependency have also been removed. The driver
never reads or writes business tables and does not require database credentials.
Use only backend-owned explicit intents for provisioning and node maintenance.
See [CORE_ARCHITECTURE.md](../../CORE_ARCHITECTURE.md).

The runtime bundle is loaded from `runtime_assets/manifest.json` under the active
`NODE_PLANE_APP_DIR` (or the source tree during development).

## Command identity and recovery

Backend mutation RPCs require a stable command identity. Profile and settings
operations use `x-node-plane-command-id`; node actions and decommissioning carry
`command_id` in their request. Command identity, method and request fingerprint
prevent duplicate execution. Reusing an identity for changed input is rejected.
Inspect uncertain outcomes before recovery; fixing configuration does not replay
an old failed command automatically.

Driver operation history is a local snapshot file with mode `0600` because
results can contain credentials. A separate OS lock prevents concurrent drivers
from opening the same journal. Corrupt or unreadable history prevents startup.
Initial journal writes must succeed before contacting the agent. Interrupted
operations are marked with `execution_interrupted`, meaning the node outcome is
unknown, and are not automatically replayed.

The driver reads v1 history and writes v2 with command identity on the next
update. Older binaries cannot read v2 history. Keep journals when replacing
binaries; deleting them removes duplicate protection.

## Run

Building requires Rust 1.89 or newer.

```bash
scripts/run_node_driver.sh
cargo test --manifest-path rust/node-driver/Cargo.toml
```

Configuration:

- `NODE_DRIVER_LISTEN_ADDR` (default `127.0.0.1:50051`).
- `NODE_DRIVER_OPERATIONS_PATH` (default
  `${NODE_PLANE_SHARED_DIR}/data/node-driver-operations.bin`).
- `NODE_AGENT_TARGETS`, for example
  `node-a=127.0.0.1:50061,node-b=10.0.0.12:50061`.
- `NODE_AGENT_CONFIG_PATH` (default `/etc/node-plane/agent.toml`): a valid local
  loopback listener can fill a missing local target without overriding explicit
  targets.
- `NODE_PLANE_APP_DIR` and `NODE_PLANE_SHARED_DIR`.

At startup the driver reads the shared `.env` without overriding environment
variables already set by its service.

Connections to agents require mutual TLS. `NODE_AGENT_CA_CERT`,
`NODE_AGENT_CLIENT_CERT` and `NODE_AGENT_CLIENT_KEY` are configured by
`scripts/setup_driver_agents.sh`. The agent certificate must match the target
address. There is no plaintext fallback.
