# node-plane-driver

Rust gRPC skeleton for the future node driver service.

Current scope:

- compiles protobuf definitions from `proto/driver/v1`;
- compiles node-agent client bindings from `proto/agent/v1`;
- exposes all current `NodeDriverClient` RPC surfaces;
- serves node/read RPCs from PostgreSQL-backed state where possible;
- can read runtime facts and remote profiles from a node agent when configured;
- can complete `ProbeNode` and `CheckPorts` immediately through a node agent;
- can attempt `OpenPorts` through a node agent and surface host-local firewall failures directly;
- can render and push `node.env` to a node agent without SSH;
- can sync the shared runtime bundle from `runtime_assets/manifest.json` through a node agent;
- can run `SyncXray` through a node agent and persist generated `xray_*` settings back into the central registry when PostgreSQL is available;
- can run `InstallDocker` through a node agent and return the completed operation to Python;
- can run `DeleteRuntime` through a node agent and update central runtime state when PostgreSQL is available;
- can orchestrate `BootstrapNode` through a node agent, including port checks, Docker install, runtime bundle sync, protocol init/deploy, and central registry updates;
- can orchestrate `ReinstallNode` by composing agent-backed runtime deletion and bootstrap flows;
- can orchestrate `FullCleanupNode`, including optional authorized key removal through the node agent;
- can execute `EnsureProfileOnNode` and `DeleteProfileFromNode` through node-agent runtime scripts and update `profile_server_state`;
- creates in-memory `Operation` records for mutating RPCs.

Runtime bundle source of truth:

- `runtime_assets/`
- `runtime_assets/manifest.json`

This crate is intentionally a thin scaffold. It is not meant to own real node
execution yet.

## Run

```bash
scripts/run_node_driver.sh
```

Optional environment variables:

- `NODE_DRIVER_LISTEN_ADDR`
- `NODE_AGENT_TARGETS`
- `POSTGRES_DSN`
- `NODE_PLANE_POSTGRES_DB`
- `NODE_PLANE_POSTGRES_USER`
- `NODE_PLANE_POSTGRES_PASSWORD`
- `NODE_PLANE_POSTGRES_PORT`

Default listen address:

- `127.0.0.1:50051`

The driver also mirrors the Python app's runtime env loading and will try to
read `${NODE_PLANE_SHARED_DIR}/.env` or the equivalent app-root `.env` before
booting.

`NODE_AGENT_TARGETS` format:

- `node-a=127.0.0.1:50061,node-b=10.0.0.12:50061`

## Next implementation targets

1. Route diagnostics and health reads more consistently through the agent.
2. Move reconcile execution out of Python into Rust.
3. Route mutating runtime actions through the agent.
4. Replace SSH-first execution with agent-first execution after bootstrap.

These are the first useful RPCs for switching the Python bot from the
in-process backend to the gRPC backend incrementally.
