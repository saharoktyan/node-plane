# node-plane-driver

Rust gRPC skeleton for the future node driver service.

Current scope:

- compiles protobuf definitions from `proto/driver/v1`;
- exposes all current `NodeDriverClient` RPC surfaces;
- returns placeholder responses for read operations;
- creates in-memory `Operation` records for mutating RPCs.

This crate is intentionally a thin scaffold. It is not meant to own real node
execution yet.

## Run

```bash
scripts/run_node_driver.sh
```

Optional environment variables:

- `NODE_DRIVER_LISTEN_ADDR`

Default listen address:

- `127.0.0.1:50051`

## Next implementation targets

1. `GetRuntimeStatus`
2. `ListRemoteProfiles`
3. `GetProfileUsage`
4. `ReconcileNode`

These are the first useful RPCs for switching the Python bot from the
in-process backend to the gRPC backend incrementally.
