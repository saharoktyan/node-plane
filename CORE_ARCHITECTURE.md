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
not promised. Retained legacy RPC/business-table paths are cleanup debt, not
permission for new clients to bypass explicit intent.

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
