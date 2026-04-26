# node-plane-driver

Rust gRPC skeleton for the future node driver service.

Current scope:

- compiles protobuf definitions from `proto/driver/v1`;
- compiles node-agent client bindings from `proto/agent/v1`;
- exposes all current `NodeDriverClient` RPC surfaces;
- serves node/read RPCs from PostgreSQL-backed state where possible;
- can read runtime facts and remote profiles from a node agent when configured;
- can complete `ProbeNode` and `CheckPorts` immediately through a node agent;
- can render and push `node.env` to a node agent without SSH;
- creates in-memory `Operation` records for mutating RPCs.

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
