# Architecture

Node Plane has two administrator interfaces, a shared backend and node-local execution. The controller runs under systemd; managed protocols run in Docker. The worker is a persistent service, reuses PostgreSQL connections, wakes on committed queue changes and performs periodic reconciliation. Startup recovery runs once; ordinary wakeups do not mark in-flight work as interrupted.

## Components

| Component | Responsibility |
| --- | --- |
| Workstation | Local connection profiles, SSH sessions, TUI state and authenticated backend requests |
| Telegram client | Telegram identity resolution, screen state, localization and delivery |
| Backend | Accounts, authorization, profiles, grants, nodes, settings and durable business operations |
| Worker | Execution/reconciliation of accepted jobs, scheduled maintenance and notifications |
| Driver | Typed gRPC execution transport, observations and durable execution records |
| Agent | Node-local runtime adapters, host actions, facts and command journals |
| PostgreSQL | Controller business state, operation queues and schema history |

The interfaces use the same backend policies. The driver does not need database credentials or reconstruct access policy. A local node follows the same agent path over loopback.

The API retains up to eight idle PostgreSQL connections. Each transaction has an exclusive connection; commit and rollback finish before reuse. Authorization reads current credential and account state on each request.

Telegram notification delivery drains durable claims, then waits up to 30 seconds through the backend. One dedicated PostgreSQL LISTEN connection per API process wakes waiting requests after delivery changes commit, including changes made by the worker. Generation tracking covers notifications arriving between the queue check and waiting. Reconnects trigger a recheck; timeout and transport failure provide periodic fallback. Wakeups carry no notification payload and never grant or replay a claim.

## Mutation lifecycle

1. Authenticate and authorize the actor; validate input and expected revision.
2. Persist the desired state and stable command identity before dispatch.
3. The worker sends a concrete intent through the driver to the agent.
4. The agent journals the command and applies runtime changes under local locks.
5. Reconcile observed results into backend read models.
6. Interfaces read status and show the result.

Duplicate identities with the same intent reuse recorded work. A changed payload under the same identity is rejected. Timeouts and interrupted execution can leave unknown outcomes. Recovery must inspect the original command before any replay decision. Navigating away does not cancel durable work.

## Trust and transport

Telegram uses a private adapter credential and resolves the Telegram principal through the backend. Workstation uses privileged SSH enrollment to bind its actual public-key fingerprint to an approved administrator; subsequent sessions use short-lived backend credentials. Local installation profiles are not backend accounts.

Driver-agent connections require mutual TLS. Keep the CA private key and driver client key on the controller. Agents receive a server certificate/key and the public CA. Restrict remote agent reachability to the controller and verify the certificate's target address.

Root access remains a privileged trust boundary. Audit attributes actions but cannot protect its records from a hostile server owner. Treat credentials, backup payloads and execution results as sensitive.

## Resource ownership

The controller installation manifest identifies owned directories, services and local database resources. Node removal captures configured runtime, agent, TLS and journal paths against a host identity. Full retirement requires independent verification of the captured resources.

Delete managed containers by verified identity and mounts. Preserve shared host packages, Docker, unrelated containers/images, firewall rules and unrelated SSH keys. Registry-only retirement records unverified host state. An agent-only registration does not acquire ownership of pre-existing VPN runtime. Full removal stops before remote cleanup if that runtime is present or its absence cannot be confirmed; it does not silently adopt or delete it.

## Persistence and compatibility

Controller desired state, driver command fingerprints and agent journals are distinct durable stores. Preserve them through updates and interrupted work. No age-based command pruning is safe without a defined stale-request horizon.

Configuration backups are snapshots of supported controller configuration; they do not replace host snapshots or remote journals. Application rollback requires compatible database revisions. See [Database](database.md), [Releases](releases.md) and [Temporary access implementation](temporary-access.md).

The controller installation UUID binds agent journals to their owner. Onboarding under another installation archives the previous journals after stopping the agent. Archival does not remove protocol access; reconcile existing runtime before granting new access.

## Node and service boundaries (pre-1.0)

Pre-1.0 boundaries separate four distinct concerns without a generalized service database schema migration:

- **Base node**: Registry entity (`backend_nodes`) representing host identity, region, metadata, and transport connection target (`local` or `ssh`). A base node exists independently of whether an agent is present or runtime services are configured.
- **Installed agent**: Host-local agent binary and systemd service. Installation evidence (`agent_installed`) is derived from durable records (such as completed agent rollout or applied revisions), never from live reachability.
- **Connectivity**: Ephemeral reachability over driver mTLS. Transport failures or timeouts are transient observations that never clear installed agent status or alter durable records.
- **VPN / Runtime services**: Containerized protocol runtimes (AmneziaWG, Xray). Runtime apply and removal intents target only VPN services. `settings_apply_pending` uses the desired protocols and confirmed applied runtime receipts; an agent-only node does not need an empty apply. Metadata/SSH edits save registry state without deploying VPN, and removing VPN does not uninstall the agent. The existing desired/applied revision columns remain compatibility counters shared by these records until the post-1.0 migration.

API admission, worker coordination and client presentation enforce these boundaries while retaining the existing schema. Host resource/connectivity alerts continue independently of VPN enablement; VPN health checks and runtime updates require applicable VPN services. Legacy diagnostic wire fields remain for compatibility and are not proof that a service is configured. Full generalization into dedicated `node_services` tables and independent per-service lifecycles is planned after 1.0.0 (see [Roadmap](../roadmap.md) and [Vaultwarden expansion](../design/service-expansion.md)).
