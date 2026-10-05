# Core architecture and execution contract

Updated: 2026-10-05. This is a design reference, not a migration checklist.
Remaining work is tracked only in [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md).

## Ownership

| Layer | Owns | Must not own |
| --- | --- | --- |
| Telegram client | Dialog state, localization, rendering, authenticated Telegram identity | Business policy and node execution |
| Python backend and worker | Accounts, authorization, profiles, node registry, desired revisions, durable business operations | Node-local protocol implementation |
| Rust driver | Typed execution transport, execution records and observations | Access policy or reconstruction of business intent |
| Node agent | Local runtime adapters, config changes, command journal and facts | Global business policy or Telegram identity |

The backend and Telegram client are separate services. The client uses HTTP;
normal node mutations use explicit backend-built gRPC intents through the driver
and agent. SSH is used for initial rollout and independent maintenance/removal
verification, not as an alternative normal provisioning path. Controller
installation, updates and host recovery have explicit owned-resource boundaries.
Local nodes use the same agent path, with a loopback listener.

The backend is authoritative for accounts and desired access. Telegram binding
is optional; VPN profiles/grants remain service-specific records. Additional
services must not be added as arbitrary values in the VPN protocol column.
See [SERVICE_EXPANSION_ARCHITECTURE.md](SERVICE_EXPANSION_ARCHITECTURE.md).

## Commands and recovery

1. Authorize the actor and validate input, including expected revisions.
2. Persist desired state, logical command identity and durable tasks before
   dispatch. The worker executes accepted tasks and records structured results.
3. Send concrete runtime parameters and revision fences to the driver/agent.
4. Update business read models from execution results and observations.
5. Render status through API reads; leaving a screen does not cancel work.

Duplicate submissions reuse logical identity. Changed payloads under the same
identity are rejected. Agent journals and revision fences protect against
repeated mutations; retention must preserve this protection. Unknown outcomes
must not be automatically replayed. A timeout is not proof of remote failure;
recovery requires observation or an explicit operator decision.

Backend job states and driver RPC operation states are distinct contracts.
Accepted backend jobs are durable even where a driver RPC is synchronous.
Unsupported RPCs fail explicitly rather than advertising pending work.
Polling is supported; live driver progress streams and remote cancellation are
not promised. Legacy driver RPCs and business-table access have been removed;
the driver requires no database credentials. Backend-owned explicit intent is
the supported provisioning and node-maintenance contract.

## Transport and secrets

Driver-to-agent transport uses mutual TLS. The CA private key and driver client
key stay on the controller; agents receive their certificate/key and the public
CA certificate. Host/IP verification must match the configured target. Rollout
renews leaf certificates near expiry; automated CA rotation/revocation is not a
completed feature. Restrict remote agent reachability to the controller.

Treat issued configs and diagnostic/history payloads as potentially secret.
Do not expose credentials in operation lists or logs. Hidden Rich sections are
not authorization boundaries. Controller cleanup must preserve unrelated host
resources; registry-only removal cannot certify remote cleanup.

## Compatibility and verification

Release identities and versioned API/protobufs exist, but arbitrary mixed
component versions are not guaranteed. Core rollback requires compatible schema
changes; configuration snapshots do not replace complete disaster recovery.
Test concurrency on actual PostgreSQL, failure/ambiguous recovery with isolated
fixtures, and node lifecycle on disposable hosts. Preserve explicit acceptance
results rather than inferring them from unit tests or stale migration boxes.

## Resource lifecycle and cleanup inventory

| Resource | Ownership evidence and current lifecycle |
| --- | --- |
| Controller checkout, releases and shared directory | Installer `.node-plane-installation.json` plus matching installation paths. Reset keeps the installation, active environment and recovery backup. Uninstall requires matching units and stops them before deleting the owned directories. |
| Controller PostgreSQL container | Installer manifest and bind mount to the installation's `shared/postgres`. An external database is not a host resource to uninstall. |
| Agent binary, service, configuration, TLS and state | Standard rollout paths and a prepared decommission identity. Drain/revocation precede runtime deletion; a detached unit stops the agent before removing its identity/journal. Independent host verification precedes registry retirement. |
| Protocol runtime directories and AWG client archives | Dedicated configured runtime root. Cleanup validates all directories before mutation, rejects symlinks and recursive deletion outside that root. Config overrides outside the root are individual files and their named backups, never whole parent directories. |
| Node-local command records | `/etc/node-plane/profile-intents.sqlite3` and its lock/fences survive protocol cleanup and reinstall. Only final decommission removes them after stopping the agent. |
| SSH authorized key | Remove the controller's exact public-key entry; retain unrelated entries and SSH server packages. Final verification requires independent access after removing the bot's key. |
| Docker images | Shared host cache, not exclusive ownership evidence. Try removing current configured images without force; retain images used by other containers. Do not remove historical global image tags merely because Node Plane once used that version. |
| Host packages, Docker daemon and firewall | Shared host facilities. Do not uninstall or broadly prune them during node removal. Existing firewall rules are not tracked as exclusively owned and remain. |

Container-name ownership and orphan discovery still need hardening: a matching
name alone does not prove a container belongs to Node Plane. Final verification
currently covers standard paths/container names, not arbitrary custom runtime
locations. Do not claim that it certifies removal of every custom or historical
artifact. These limitations remain in the active backlog.

Recovery of an interrupted removal uses the stored command and phase. Observe
the host first; an uncertain agent uninstall requires independent verification,
not a blind second uninstall. If the host is unavailable, registry-only removal
keeps a retirement record and explicitly leaves remote state unverified. If the
host later returns, treat its journals/configs as existing state and inspect or
finish decommissioning before enrolling a fresh agent. No automatic orphan
garbage collector or host-wide Docker prune is supported.

## Retention and journal growth

| Data | Current retention and deletion rule |
| --- | --- |
| Backend operations, task identities and retirement evidence | No age-based pruning. Retain unresolved work, desired/applied revision evidence and idempotency keys; retirement hides the node but preserves execution evidence. |
| Driver `data/node-driver-operations.bin` and lock | No automatic pruning. The complete operation map is persisted as a snapshot on updates, so memory, file size and write cost grow with history. Keep both command fingerprints and results through restarts/upgrades. Never remove the active lock file. |
| Agent SQLite journal, fences and removal marker | No automatic command pruning. Completed, retired and uncertain identities prevent delayed requests from executing again. Protocol cleanup/reinstall must preserve the journal and node lock. |
| Config issuance records | Authorization expires after 15 minutes; this is access expiry, not deletion. The record remains for idempotent lookup. AWG credentials are generated/refreshed at delivery; issuance metadata stores a digest, while provisioning results/journals can still contain private configs. |
| Issued Telegram artifacts | Delivered messages/files belong to the client conversation, not a server-side expiring download cache. Revocation disables runtime access but cannot erase copies already saved by a user. |
| Backend configuration backups | Configurable keep count (5/10/20, default 10). Pre-restore and pre-reset/removal snapshots bypass pruning for recovery. Snapshots can contain sensitive provisioning results and must be protected. |
| Release directories | Explicit release cleanup with protected active/rollback targets; never treat shared state or journals as disposable release contents. |

There is currently no defined maximum retry/delivery horizon that makes command
identity deletion safe. Therefore a storage-pressure fix must not truncate
command tables, delete journals, clear blocked tasks or expire idempotency keys
by age. Monitor database/journal/backup disk usage. To reduce secret payload
retention later, separate payloads from durable identity/fingerprint/tombstones,
prove recovery and stale-request rejection, then introduce explicit retention.
Backing up and restoring controller configuration does not reset remote command
history; preserve node fences and reconcile before provisioning again.
