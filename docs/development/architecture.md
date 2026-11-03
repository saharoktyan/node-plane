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

Delete managed containers by verified identity and mounts. Preserve shared host packages, Docker, unrelated containers/images, firewall rules and unrelated SSH keys. Registry-only retirement records unverified host state. An agent-only registration does not acquire ownership of pre-existing VPN runtime. A server whose node.env names another node, for example one left by another installation, is refused before anything on it changes: the agent rollout checks it right after SSH access, the agent refuses to write runtime files or node.env for it, and node operations check it before journaling a command. Such a refusal is a known outcome: the operation fails with `runtime_owner_conflict` instead of blocking. Full removal stops before remote cleanup if that runtime is present or its absence cannot be confirmed; it does not silently adopt or delete it.

## Persistence and compatibility

Controller desired state, driver command fingerprints and agent journals are distinct durable stores. Preserve them through updates and interrupted work. No age-based command pruning is safe without a defined stale-request horizon.

Configuration backups are snapshots of supported controller configuration; they do not replace host snapshots or remote journals. Application rollback requires compatible database revisions. See [Database](database.md), [Releases](releases.md) and [Temporary access implementation](temporary-access.md).

The controller installation UUID binds agent journals to their owner. Onboarding under another installation archives the previous journals after stopping the agent. Archival does not remove protocol access; reconcile existing runtime before granting new access.

## Node and service boundaries (pre-1.0)

Pre-1.0 boundaries separate four distinct concerns without a generalized service database schema migration:

- **Base node**: Registry entity (`backend_nodes`) representing host identity, region, metadata, and transport connection target (`local` or `ssh`). A base node exists independently of whether an agent is present or runtime services are configured.
- **Installed agent**: Host-local agent binary and systemd service. Installation evidence (`agent_installed`) is derived from durable records (such as completed agent rollout or applied revisions), never from live reachability.
- **Connectivity**: Ephemeral reachability over driver mTLS. Transport failures or timeouts are transient observations that never clear installed agent status or alter durable records.
- **VPN / Runtime services**: Containerized protocol runtimes (AmneziaWG, Xray). Runtime apply and removal intents target only VPN services. `settings_apply_pending` uses the desired protocols and confirmed applied runtime receipts; an agent-only node does not need an empty apply. Metadata/SSH edits save registry state without deploying VPN, and removing VPN does not uninstall the agent. The existing desired/applied revision columns remain compatibility counters shared by these records until the planned service-model migration.

API admission, worker coordination and client presentation enforce these boundaries while retaining the existing schema. Host resource/connectivity alerts continue independently of VPN enablement; VPN health checks and runtime updates require applicable VPN services. Legacy diagnostic wire fields remain for compatibility and are not proof that a service is configured. New VPN capabilities must preserve these boundaries and explicit account/profile ownership. After that capability work, the planned refactor completes both native Agent execution and the storage/workflow migration to independent node, Agent and service lifecycles. Broader non-VPN service implementations and public extension modules remain separate decisions; priorities and release scope follow the [Roadmap](../roadmap.md).

## VPN policies

`backend/vpn_policies.py` stores policy values for VPN capabilities such as limits and runtime toggles in `backend_vpn_policies`. Policy values are not added as new columns on nodes or profiles. A feature registers each key with its allowed subjects (`installation`, `profile`, `device`), scopes (global, node, node and protocol), validator and default. Writes are idempotent commands with per-value revisions, serialized per subject.

`effective()` resolves one target: device values override profile values, and profile values override installation values. Within a subject, a node and protocol value overrides a node value, which overrides a global value. Protocol adapters receive only the resolved values. Node and protocol scopes become service-instance scopes in the planned service-model migration. Values are included in configuration backups and removed with their profile, device or node.

A key with a node runtime `setting` belongs to the installation (with node scopes later). Changing it increments the desired revision of every node running an affected protocol. Node settings intents then carry the resolved non-default value, for example `xray_bittorrent`. Default values are omitted, so agents that do not know a newer setting still accept ordinary intents.

Limits that a node enforces continuously are delivered differently. `backend/connection_limits.py` resolves `connections.*` for every active VLESS client of a node into one snapshot with a revision and sends it with the `ApplyAdmissionPolicy` agent call every minute. The agent journals the snapshot and ignores revisions that are not newer, so resending is harmless. `backend_node_admission` records the revision sent and the revision in force on each node. The agent's admission loop (`protocols/xray/admission.rs`) reads Xray's online IP lists and blocks evicted sources with routing rules; it keeps working while the controller is unreachable. After each delivery the controller calls `ObserveSessions`: the agent returns eviction events from an in-memory buffer numbered per agent run, without addresses, and the source counts of limited clients. The controller keeps the counts in `backend_vpn_session_observations` and the events in `backend_admission_events` for notifications and the link-sharing alert.

Speed limits (`backend/speed_limits.py`) use the same delivery (`backend/node_snapshots.py`): one snapshot of `speed.*` per node for every active Xray client and AWG peer, sent with `ApplyRatePolicy`, recorded in `backend_node_shaping` together with the agent's outcome per protocol. One agent loop (`enforcement.rs`) runs admission and then shaping (`shaping/`). Xray has no per-user shaping: each limited client gets a freedom outbound with its own `SO_MARK` and a routing rule, and the kernel shapes by that mark on the uplink with HTB behind ifb devices (`protocols/xray/rate.rs`); conntrack carries the mark to the download direction. Rate rules always follow eviction rules, or an evicted source would bypass its block. AWG peers are shaped by tunnel address inside the AWG container's network namespace (`protocols/awg/rate.rs`). Each client keeps its class number while limited, so changed limits are applied to its classes in place; the agent rebuilds a tree only when it is gone (a reboot or a container restart), its interfaces changed or an in-place change failed. AWG peers retired by a key reissue keep their device's limits until every node removed them. `SHAPING_UPLINKS` in node.env overrides the uplinks found from the default routes. `tests/e2e/shaping` checks this against a real kernel and Xray in a privileged container.

Traffic quotas (`backend/quotas.py`) are decided by the controller, since a quota spans all nodes. The worker keeps one open row per profile in `backend_quota_periods` (a partial unique index prevents a second after a restart), accrues collected traffic deltas into it, and closes it on the reset strategy. An exhausted period with `freeze` sets `blocked`, which every access decision treats like an expiry (`quotas.BLOCKED_SQL`), and records a profile operation that revokes peers; closing the period records one that restores them.
