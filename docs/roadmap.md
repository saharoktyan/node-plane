# Roadmap

This roadmap describes public priorities, not guaranteed release contents. Beta feedback, security work and operational evidence may change the order.

## Public beta preparation

The public beta is for learning how Node Plane behaves in independent environments and which improvements matter most. The maintainer has already exercised the core installation, configuration delivery and administration workflows.

- Validate the separate `beta` channel (`0.5.0-beta.1` baseline), isolated from experimental `dev` alpha builds and `main` stable builds across Workstation, controller and node updates.
- Preserve installation identities, credentials, configuration, revocations and operation history through supported updates and recovery.
- Recruit users to install on a test VPS, report what worked or failed, and describe confusing workflows or missing scenarios.
- Finish the security, release-validation and documentation gates needed for a responsible public beta.

## Near-term development priorities

### 1. Expand VPN capabilities

Make the most important everyday VPN workflows easier and more complete:

- renewable configuration subscriptions for supported clients;
- traffic quotas;
- devices and device limits for VLESS and AmneziaWG, and a limit on simultaneous connections per VLESS device configuration;
- per-profile speed limits;
- stronger active-session revocation;
- a manual server-chain builder.

The exact protocol behavior, supported clients and release scope will be defined and tested before each feature is presented as available.

New VPN capabilities must already preserve separate node, Agent, service-instance and account/profile lifecycles. Keep policy and accounting separate from protocol execution, with explicit ownership of settings, counters and resources. New node execution paths belong in the Rust Agent. This allows the later model migration to reuse the VPN implementations.

### 2. Complete the Agent and service-model refactor

Reduce runtime dependence on Python and shell orchestration by moving the remaining node execution paths into the Rust Agent while preserving command identity, ownership, recovery, journals and existing VPN behavior.

Separately migrate controller storage and workflows so a node, its installed Agent and each managed service have independent identities and lifecycles. Keep user accounts independent of VPN profiles and future service profiles. Both migrations follow the VPN capability work; a public plugin framework and custom RBAC are outside this refactor.

### 3. Evaluate one non-VPN service

Select one non-VPN service as an architecture exercise after the higher-priority VPN work. Candidates may include Grafana/Loki or Vaultwarden, but neither is selected or promised. The evaluation will test service lifecycle, accounts and access, secrets, updates, backups, recovery and owned-resource removal without weakening the VPN model.

## Stable 1.0.0 acceptance

The stable feature cut will be chosen after beta feedback. Regardless of feature scope, stable release work must verify clean installation, supported updates, backup/restore, access expiry and revocation, node recovery, release artifacts and channel isolation on disposable environments.

Workstation behavior on supported Linux, macOS and Windows environments and Telegram administration flows will be checked for the documented workflows. Untested cases and current limitations will remain explicit.

## Later scope

Later work depends on demand and beta evidence. Possible areas include additional VPN protocols, richer client tooling, observability and other managed services. New service work must preserve independent service data and secrets, authorization boundaries, durable operations and safe recovery.
