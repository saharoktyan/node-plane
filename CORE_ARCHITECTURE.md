# Core architecture and execution contract

Updated: 2026-09-24.

This document defines the implementation boundary for the shared free core and
future Pro modules. It supersedes the ownership recommendations in the older
driver scaffold document. Requirements below are targets unless explicitly
marked as implemented.

## Ownership

| Layer | Owns | Must not own |
| --- | --- | --- |
| Telegram adapter | Dialog state, localization, rendering, authenticated Telegram identity | Access policy, provisioning decisions, transport execution |
| Python backend | Authorization, profiles, node registry, desired state, business scenarios, feature availability | SSH commands and node-local mutations |
| Rust driver | Execution, transport, operation lifecycle, observations | Telegram identity, subscription policy, licensing decisions |
| Node agent | Local runtime adapters, configuration changes, runtime facts | Global desired state, business database, product tiers |

Backend initially remains in the same Python process as the bot. Its entry
points must accept explicit actor and scenario input, without Telegram Update,
CallbackContext, or localized output. Every entry point checks permissions;
hiding a button is not authorization. An internal scheduler uses an explicitly
defined service actor rather than pretending to be a Telegram user.

Pro extends backend scenarios and interfaces. It does not fork the driver or
agent. Core provides technical primitives such as reading counters and removing
access; Pro owns device accounting and traffic-limit policy. Core must remain
usable without the licensing service.

## Data boundary

Backend is authoritative for users, profiles, access methods, node configuration
and desired state. Driver owns execution records and observed runtime facts.
Agent owns the application of runtime files and local commands.

Target flow for provisioning:

1. Backend authorizes the actor and validates the request.
2. Backend records desired state and constructs a concrete execution command.
3. Driver applies the command through the selected agent.
4. Backend consumes the structured result and updates its provisioning read model.
5. The interface renders that read model and operation state.

A future command must carry node identity, explicit runtime/profile parameters,
an idempotency key and desired-state revision. The driver must not reconstruct
business intent from profile/access tables. Concrete protobuf evolution follows
one scenario at a time, with compatibility for the existing client during rollout.

Current migration debt: driver still queries and updates business PostgreSQL
tables, computes desired profile state during reconcile, and renders node.env.
Python still has direct runtime execution paths. These remain until equivalent
backend/driver paths have been exercised; they are not the target design.

## Operation contract

Target lifecycle:

```text
accepted → PENDING → RUNNING → SUCCEEDED | FAILED | CANCELLED
```

- `PENDING` means actual accepted work backed by an executor. It must never mean
  unsupported functionality or a missing transport.
- A returned operation ID must be retrievable through `GetOperation`.
- Terminal results preserve node/profile identity, timestamps, machine-readable
  result and error information. Text is for diagnostics, not business decisions.
- Unsupported RPCs return gRPC `UNIMPLEMENTED` before accepting work.
- Invalid requests are rejected before node mutation.
- A transport timeout does not prove that the node mutation failed. Do not
  automatically repeat a mutating command until idempotency/reconciliation makes
  the retry safe.
- Future retries reuse the same logical command identity. Concurrent mutations
  on the same node must be serialized or explicitly coordinated.
- Durable acceptance, restart recovery, retention and progress delivery must be
  implemented before switching long operations to asynchronous acceptance.
- Results may contain client credentials, including AWG config. The local
  history file uses mode `0600`; future streams need access control and must
  not log these payloads.

### Implemented now

- Agent-dependent fallback paths return a terminal `FAILED` operation with
  `error.code=agent_not_configured` when no agent target is configured.
- This configuration error is not automatically retryable. Configure the target
  before issuing a new request.
- `CollectTrafficSnapshot` and `WatchNodeHealth` explicitly return
  `UNIMPLEMENTED`; the existing Python traffic collector remains unchanged.
- `WatchOperation` returns the recorded terminal result and closes. Unknown IDs
  return `NOT_FOUND`; live progress for nonterminal records is not implemented.
- `ListOperations` filters before limiting and orders by update time descending,
  with operation ID as a deterministic tie-breaker.
- `SyncNodeEnv`, `ProbeNode`, `CheckPorts`, `OpenPorts` and `InstallDocker`
  persist `RUNNING` before contacting a configured agent, then update the same
  record at completion. Failure to persist the start prevents agent dispatch.
- Dropping an unfinished execution or recovering it at startup records
  `FAILED` with `execution_interrupted` and `retryable=false`: the node outcome
  is unknown and must be inspected before another command. Recovery never
  automatically repeats a remote action.
- A separate advisory lock on the local history allows only one driver writer.

### Still not implemented

Execution currently completes inside the mutating RPC. Actions outside the
five NodeService commands still persist only terminal records. A crash can
leave the mutation applied without an ID returned to the caller. There is no
durable queue, live operation subscription, remote cancellation, per-node
serialization or idempotent retry mechanism. A local cancellation does not
prove that the remote action stopped.
Existing runtime failures do not all have structured error codes yet.

The agent transport now uses mutual TLS. The controller's CA private key and
driver client key stay on the controller; rollout sends each node its server
certificate/key and the public CA certificate. The client verifies the agent
certificate against the host/IP in `NODE_AGENT_TARGETS`, and the agent requires
a client certificate signed by this CA. DNS, IPv4 and bracketed IPv6 targets
are supported by the rollout. The listener still binds to the
configured address (rollout currently uses `0.0.0.0`); network firewall rules
should restrict access to the controller where possible. Leaf certificates
renew on a rollout when less than 30 days remain. CA rotation and certificate
revocation still require a manual rollout procedure.

## Acceptance gates for subsequent changes

1. Transport: review mutual TLS behavior with correct, wrong and missing
   credentials; test rollout and certificate rotation on a disposable node.
2. Operations: persistent acceptance, restart recovery, duplicate submission and
   ambiguous timeout tests before automatic retries or asynchronous execution.
3. Backend extraction: one scenario callable without Telegram, shared permission
   checks, driver receiving explicit intent, and no direct shell bypass.
4. Core integration: real PostgreSQL and a disposable node covering bootstrap,
   Xray/AWG create/edit/delete, drift, reinstall, cleanup and failures.

Unit tests do not replace the disposable-node gate. Existing Pro candidates and
PTB v22 migration remain outside this core implementation stage.
