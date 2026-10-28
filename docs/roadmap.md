# Roadmap

This file lists planned work and acceptance gates. User guides describe implemented workflows; release history belongs in [CHANGELOG](../CHANGELOG.md).

## Interactive Quick Start Guides

Workstation F1 and the administrator's Telegram home menu open guides derived from controller state. They navigate to existing screens for defaults, node creation, setup and grants; configuration delivery uses Telegram. Completed operations are skipped on reopening. Remaining guide work: visual acceptance across supported clients and improving the first-installation connection prompts after tester feedback.

## Release acceptance

Before 1.0, accept the limited node/service boundary: agent-only install/probe/removal, host alerts independent of VPN enablement, metadata-only saves without runtime jobs, and explicit VPN apply/removal ownership. Keep the shared revision/schema compatibility; defer generalized service tables and per-service profiles until after the release.

Verify clean installation and update on disposable hosts, complete local/SSH node setup and removal, backup restore, profile revocation and temporary-access expiry. Test notifications/alerts and automatic update checks on real installations.

Run Workstation acceptance on Linux, macOS and Windows: non-admin local use, PATH, SSH/session reuse, clipboard, focus, scroll and resize. Check Telegram on desktop, Android and iOS. Record untested cases explicitly before a stable release.

## After 1.0.0

- **Generalized `node_services` architecture**:
  - Decouple service lifecycles from the base `backend_nodes` table into a dedicated `node_services` schema with independent desired and applied revisions per service (e.g., AmneziaWG, Xray, Vaultwarden).
  - Track per-service lifecycle states (provisioning, active, updating, draining, stopped, removed) rather than multiplexing all protocol configurations and revisions into monolithic node settings JSON.
  - Separate per-service account profiles and access grants: isolate VPN client configurations from service-specific user identities (such as Vaultwarden vault accounts), preventing non-VPN secrets from leaking into VPN profiles, logs, or audit records.
  - Preserve architectural component boundaries: Backend owns authorization, desired state, and admission; Worker reconciles durable lifecycle jobs asynchronously; Driver provides authenticated typed gRPC transport and stable command identities without business database access; Agent manages host-local service adapters, local locking, and durable command journals.
- [Vaultwarden lifecycle support](design/service-expansion.md) for user password vaults. Separately evaluate controller secret storage; keep bootstrap/recovery credentials independently available and isolate service secrets from user vaults. Vaultwarden is not assumed to provide a machine secrets manager.
- Add metrics collection for use with Grafana. Evaluate a persistent Agent–Driver gRPC stream for metrics and events, with reconnection and state reconciliation; operation results and critical events require durable delivery rather than best-effort sampling.
- Start daisy-chain support with limited, manually configured node chains, route validation and diagnostics. Later consider prepared fallback routes and automatic recovery, including failure detection, coordinated switching and rollback. Preserving existing sessions is a separate requirement, not an initial guarantee.
- Evaluate established-session revocation in the Xray runtime. Current VLESS revocation blocks new connections; existing connections may remain until disconnect. Any implementation must preserve other users' sessions and shared listeners.
- Define safe retention for long-lived command journals without losing recovery or duplicate protection.
