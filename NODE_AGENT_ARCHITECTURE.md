# Node Agent Architecture

## Goal

Introduce a small Rust process that runs on every managed node and becomes the
local execution endpoint for the central `node-plane-driver`.

This agent should replace direct SSH-heavy runtime manipulation with a stable
node-local control surface.

## Responsibilities

The node agent should own only node-local concerns:

- execute runtime commands on the local machine;
- read local runtime metadata and config files;
- collect traffic and health telemetry;
- expose local capability and health facts;
- stream operation progress back to the central driver;
- apply node-local file mutations in a controlled way.

The node agent should not know about:

- Telegram handlers;
- user-facing text;
- profile access policy;
- business ownership rules;
- global reconciliation planning across nodes.

## Split With Central Driver

### Central Driver

- receives gRPC requests from the Python bot;
- validates intent against registry/business state;
- owns operation lifecycle and retries;
- computes desired state;
- coordinates one or more nodes;
- stores observed operational state in PostgreSQL.

### Node Agent

- executes concrete local actions on one node;
- reports local runtime facts;
- returns structured stdout/stderr and machine-readable summaries;
- maintains a local work directory and runtime adapters.

## Recommended Transport

Recommended shape:

1. Python bot talks only to central driver.
2. Central driver talks to node agents.
3. Node agents do not talk directly to the bot.

Initial transport options:

- short term: SSH bootstrap plus reverse or direct gRPC connection;
- medium term: mTLS gRPC between central driver and node agent;
- optional fallback: local command runner behind a trait for early bootstrap.

## Bootstrap Model

The first install can still use SSH.

After bootstrap:

1. central driver installs the agent binary;
2. central driver writes node-local config;
3. agent starts under systemd;
4. central driver switches the node to agent-backed execution;
5. direct SSH becomes break-glass only.

## Agent Surface

The node agent does not need the full public bot-facing API.

Minimal useful local surface:

- `GetRuntimeFacts`
- `GetNodeHealth`
- `ListRemoteProfiles`
- `CollectTrafficSnapshot`
- `ApplyRuntimeSync`
- `ApplyXraySync`
- `BootstrapRuntime`
- `DeleteRuntime`
- `OpenPorts`
- `InstallDocker`
- `RunDiagnostics`

## Local Modules

Suggested internal layout:

- `config`
- `runtime`
- `telemetry`
- `health`
- `executor`
- `transport`
- `files`

The important rule is to keep side effects behind narrow interfaces so the
central driver can call the same logical operation without caring whether the
underlying transport is SSH or agent RPC.

## Rollout Order

1. Add node-agent crate and config model.
2. Add heartbeat/runtime-facts loop.
3. Add a minimal local RPC surface.
4. Teach central driver to detect agent availability.
5. Route read-only operations through the agent.
6. Route mutating runtime operations through the agent.
7. Demote direct SSH to bootstrap-only or emergency-only usage.

## Immediate Next Steps

1. Add central-driver fallback rules for `agent` vs `ssh`.
2. Expand the agent RPC surface from read-only operations to mutating runtime actions.
3. Add authenticated transport and node registration flow.
4. Move central reconcile logic onto agent-backed observations.
