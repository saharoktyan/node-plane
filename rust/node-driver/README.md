# node-plane-driver

Rust gRPC skeleton for the future node driver service.

Current scope:

- compiles protobuf definitions from `proto/driver/v1`;
- exposes all current `NodeDriverClient` RPC surfaces;
- serves node/read RPCs from PostgreSQL-backed state where possible;
- creates in-memory `Operation` records for mutating RPCs.

This crate is intentionally a thin scaffold. It is not meant to own real node
execution yet.

## Run

```bash
scripts/run_node_driver.sh
```

Optional environment variables:

- `NODE_DRIVER_LISTEN_ADDR`
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

## Next implementation targets

1. Replace note-derived runtime status with node-reported runtime metadata.
2. Replace `profile_server_state`-backed remote inventory with actual remote observations.
3. Move reconcile execution out of Python into Rust.
4. Introduce the node-side transport/agent path.

These are the first useful RPCs for switching the Python bot from the
in-process backend to the gRPC backend incrementally.
