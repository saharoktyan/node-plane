# node-plane-driver

Rust gRPC execution service for node operations, currently under migration.

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
- can read AWG entropy and regenerate it through the node agent, recording regeneration as a durable command;
- can execute `EnsureProfileOnNode` and `DeleteProfileFromNode` through node-agent runtime scripts and update `profile_server_state`;
- persists the start and terminal result of all 16 implemented operation RPCs.

Runtime bundle source of truth:

- `runtime_assets/`
- `runtime_assets/manifest.json`

This service executes real node mutations. It still reads business state from
PostgreSQL; moving that responsibility into the Python backend is planned in
[CORE_ARCHITECTURE.md](../../CORE_ARCHITECTURE.md).

## Operation behavior

Agent-dependent actions without a configured target return a terminal `FAILED`
operation with `error.code=agent_not_configured`. They do not enqueue work.
`CollectTrafficSnapshot` and `WatchNodeHealth` return `UNIMPLEMENTED`.

`WatchOperation` emits the recorded terminal result and closes; unknown IDs
return `NOT_FOUND`. Live progress is not implemented. `ListOperations` returns
the most recently updated matching records first.

Operations are still executed inside the request. All implemented operation
RPCs in NodeService, ProvisioningService and RuntimeService persist `RUNNING`
before execution. Completion updates the same ID and retains its start time and
structured result. If the initial write fails, the action is rejected before
contacting the agent. Missing-agent paths complete the record with a terminal
failure. Reinstall and profile reconciliation also record
their nested bootstrap/node-reconciliation operations under separate IDs.

Dropping an unfinished execution (for example, when its RPC is cancelled) marks
it `FAILED` with `error.code=execution_interrupted` and `retryable=false`.
Startup applies the same recovery to persisted `PENDING`/`RUNNING` records.
This means the node outcome is unknown; the remote action may have completed or
may still be running. Inspect the node before issuing another command. Work is
not automatically resumed or repeated. `GetOperation` and `ListOperations`
expose the recorded state; live `WatchOperation` remains unsupported.

History is a local snapshot file. A corrupt or unreadable file prevents startup
rather than silently discarding history. It can contain client credentials in
`result_json`, so the driver creates it with mode `0600`. A separate `.lock`
file prevents two driver processes from opening the same history concurrently;
keep it in place while the driver is running. The OS releases the lock when the
process exits, including after a crash.

An interrupted caller can resubmit with its saved command key to recover the
operation ID, or use `ListOperations` to inspect recent work. Durable
asynchronous acceptance, per-node coordination and retention remain pending.

## Command identity

All 16 operation RPCs accept optional metadata `x-node-plane-command-id`.
The key is a single 1–128 character token using ASCII letters, digits, `-`, `_`,
`.` and `:`. Its namespace spans the driver's history, across RPC methods.
The driver atomically stores the key, method and SHA-256 of the decoded request's
protobuf encoding with the initial operation. Repeated fields retain their
order; changed request parameters require a new key.

Reusing the key with the same request returns the existing operation ID without
executing the action again, including while it is `RUNNING` and after restart.
Reusing it for a different request or method returns `ALREADY_EXISTS`.
Failed and interrupted results are also retained: fixing configuration does
not cause an old key to execute again. A new intentional action requires a new
key. Requests without metadata retain independent execution semantics.

`GrpcNodeDriverClient` exposes `command_id` as an optional keyword on these
methods, for example `driver.install_docker("node-a", command_id=saved_key)`.
The backend persists the key and request before the first call for server
actions in the admin wizard and the `/bootstrapserver`, `/probeserver` and
`/syncxrayserver` commands. It maps the Telegram update ID to one key in
PostgreSQL, then records the operation ID if a response arrives. Redelivery of
the same update retrieves the prior result or resubmits with the same key after
an ambiguous RPC failure. Profile provisioning and other bot flows do not yet
use the command journal. The Python client does not automatically retry and reports RPC errors as
`retryable=false`. This requires an updated driver; older drivers ignore the
metadata. Deduplication lasts as long as the operation history is retained.

The driver reads v1 history and writes v2 on the next update. v2 adds command
identity alongside the operation; existing IDs and results are preserved.
Older driver binaries cannot read v2 history.

## Run

Building requires Rust 1.89 or newer.

```bash
scripts/run_node_driver.sh
```

Optional environment variables:

- `NODE_DRIVER_LISTEN_ADDR`
- `NODE_DRIVER_OPERATIONS_PATH` (defaults to `${NODE_PLANE_SHARED_DIR}/data/node-driver-operations.bin`)
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

The driver connects to agents over mutual TLS. Set `NODE_AGENT_CA_CERT`,
`NODE_AGENT_CLIENT_CERT` and `NODE_AGENT_CLIENT_KEY`; these paths are written
to the shared environment by `scripts/setup_driver_agents.sh`. The agent
certificate must match the DNS name or IPv4 address in `NODE_AGENT_TARGETS`.
There is no plaintext fallback.

## Next implementation targets

1. Review mutual TLS with valid, missing and wrong credentials.
2. Persist command identities in backend workflows and add node-state
   reconciliation for interrupted work before enabling automatic retries.
3. Move business desired-state construction into backend commands.
4. Remove remaining direct Python execution paths after parity validation.

Run isolated driver tests without a database or managed node:

```bash
cargo test --manifest-path rust/node-driver/Cargo.toml
```
