# Roadmap

This file records priorities, dependencies and acceptance gates. User guides describe implemented workflows; release history belongs in [CHANGELOG](../CHANGELOG.md). All work below is planned unless explicitly described as existing behavior.

## Public beta preparation

The public beta precedes stable 1.0.0. The maintainer has exercised the current installation, configuration delivery and administration workflows; public testing is for independent environments and product discovery, not a substitute for that initial validation. Learn where installation and daily use differ across VPS providers, operating systems, networks and clients; identify confusing workflows, missing use cases and the reasons users continue or stop using Node Plane. Use this evidence to reorder development priorities and decide the stable feature cut. Planned features are not automatically release blockers.

### Release channels and installation

- Keep experimental development and CI builds on `dev`; use a stabilization branch such as `release/1.0` for selected fixes and public beta candidates. Reserve `main` and the stable channel for stable releases.
- Implement an independent public `beta` installation/update channel. Today alpha, beta and rc builds share the development channel; creating a Git branch alone does not isolate release discovery. Update installers, Workstation self-update, controller/Driver/Agent delivery and publication rules together.
- Promote complete releases with fixed tags, matching component versions, manifests and checksums. Verify that beta installations never select experimental dev releases, while deliberately promoted beta updates remain available. Check installation and updates on Linux, macOS and Windows.

### Updater bootstrap and compatibility baseline

Use a small bootstrap that obtains and verifies the updater belonging to the selected target release, then runs it separately to verify and update the installation. The oldest updater should not need a permanent inventory of every future application module. Define the bootstrap compatibility contract and artifact identity, provenance, size and safe extraction checks before implementation.

Preserve operation identities, schema compatibility checks before stopping services, interrupted-operation recovery and documented rollback boundaries. Downloading and executing an arbitrary mutable latest updater is not the intended mechanism.

Start supported upgrade/migration guarantees with the first public beta; earlier private test installations may require clean reinstallation. Subsequent supported updates must preserve identities, credentials, revocations, configuration and command journals, with tested migration and backup/restore paths. Keep released migration sources immutable; introduce schema changes through new revisions.

### Onboarding, presentation and feedback

Workstation F1 and the administrator's Telegram home menu already expose state-derived Quick Start guides. Finish supported-client acceptance and improve first-installation prompts, including obtaining the Telegram bot token and administrator ID.

Keep the README aligned with the public candidate: explain installation from Workstation over SSH without manual server-console commands, actual advantages and tradeoffs, and an honest scenario comparison with existing panels. Distinguish implemented/verified behavior, current limitations, planned beta improvements and longer-term ideas. Replace development-channel installation instructions when the isolated public beta channel is implemented; avoid unsupported security or IP-blocking claims.

Close prioritized security findings before public recruitment. Provide a reproducible candidate, documented limitations and one clear feedback path. Evaluate independent installation, daily administration and reasons users continue or stop using the product.

## Near-term development priorities

The order is bounded foundations, existing VPN capabilities, then managed observability. Design before implementation; deliver and verify small complete steps.

### 1. Bounded Agent and service refactor

- **Design first:** define node, Agent and service identities, lifecycle operations and internal adapter boundaries before porting runtime logic.
- **Incremental native runtime migration:** remove Python-interpreter and shell-script dependencies from the Rust Agent execution path by porting runtime orchestration into Rust Agent modules. Move code when it is changed for feature work or reliability fixes, and migrate remaining paths in small, independently verifiable steps rather than requiring a one-time rewrite. Scope includes install/config/apply/remove, Xray/AWG user/device handling, inspection, recovery/force-recovery, expiry monitoring and startup enforcement. Embedded scripts still executed through an interpreter do not complete this work. Docker and protocol binaries remain explicit external dependencies; controller/Telegram Python and unrelated host bootstrap tooling are outside this migration.
- **Separate current services:** after the native migration, separate the node registry, installed Agent state, connectivity observations and existing Xray/AWG service instances. Give services independent configuration, desired/applied state and revisions, lifecycle jobs and owned resources, using a scoped model such as `node_services`. Coordinate controller/worker logic, Driver/Agent contracts, journals and API/UI read models. Removing VPN must preserve the Agent and unrelated resources.
- **Preserve guarantees:** Backend owns authorization, admission and desired state; Worker reconciles durable jobs; Driver provides authenticated transport and command identity without business DB access; Agent owns local execution, locking, ownership and journals. Preserve fingerprints, revision fencing, duplicate protection, revocation/expiry and recovery after interruption or controller outage.
- **Review account coupling:** accounts and VPN profiles are already separate records. Review automatic profile creation, single-profile API fields, deletion initiating account deletion and legacy status repair. The current one-account/one-VPN-profile rule is not itself a bug. Preserve documented semantics until deliberately migrated; defer arbitrary service enrollment/profile machinery until additional services require it.
- **Scope limit:** retain an internal adapter contract for built-in protocols. Custom RBAC roles, public plugins, generic service/UI schemas and plugin execution engines are deferred. Decide on broader modularity from concrete service needs and implementation cost.

Acceptance: existing Xray/AWG operations pass on isolated hosts without shipped shell/Python orchestration in the Agent execution path. Check runtime traffic, expiry, restarts, interrupted commands, controller outages, Agent-only nodes and independent service removal. Verify supported public-beta data/journal migration and restore behavior.

### 2. Existing Xray and AmneziaWG capabilities

Prioritize practical parity in important VPN administration scenarios. During detailed design, compare current 3x-ui documentation/source against Node Plane for subscriptions, quota/reset behavior, IP/device controls, traffic statistics, expiry/freeze/revocation and routing/chains. Record actual gaps and prioritize them; a feature list alone does not establish equivalent behavior.

- **Configuration subscriptions:** support user/device-scoped delivery for Xray and AWG. Declare supported clients/platforms/formats and test actual URL import and refresh. A static `.conf` or `vpn://` key is not proof of renewable subscription support. Preserve device identities and enforce subscription credential rotation/revocation, grants and expiry.
- **Monthly traffic quotas:** prioritize byte quotas per account/VPN profile. Define the monthly reset window, upload/download accounting scope across nodes/protocols/devices, counter accuracy and persistence, exhaustion enforcement and re-enabling. Test whether exhaustion stops new connections or established traffic and document the actual contract. Per-IP byte quotas are a separate optional follow-up, not a requirement for the first quota implementation.
- **Concurrent connection limits:** add a configurable simultaneous-use limit per account/profile. Define sessions/activity and enforcement separately for Xray and AWG; tunneled streams, configured devices, active peers and source IP counts are not interchangeable. Test concurrent admission, release of inactive/disconnected sessions, restarts and unaffected users. Concurrent-IP limits remain a separate design decision.
- **Speed limits:** start with an aggregate per-profile limit on each node, independently configurable for upload and download. All devices/flows belonging to that profile on the node share its budget. Validate authenticated-user attribution and enforcement separately for Xray and AWG before promising support; a public-IP shaper alone does not establish a profile limit. Test multiple flows/devices, shared public IPs, both directions and restarts. Global cross-node rate budgets and borrowing/fair-share policies remain later design topics.
- **Basic server chains:** create manual chains in Workstation TUI and Telegram: user -> entry A -> zero or more intermediates -> exit C. Validate supported protocol combinations, ownership, credentials, loops, coordinated apply/rollback and actual traffic.
- **Revocation:** evaluate fine-grained active-session termination in the shared Xray runtime. Current VLESS revocation blocks new connections; established sessions may remain until disconnect. Any stronger mechanism must preserve other users and shared listeners.

Acceptance: test subscription refresh with declared clients, monthly quota accounting/enforcement/reset across restarts, concurrent-session and per-profile/per-node speed limits, independent users/devices and manual chain traffic and recovery. Automatic chain failover is not required.

### 3. Managed Grafana and Loki

After the bounded refactor and current VPN improvements, add service adapters for deployment, configuration, health, update, backup, restore and owned-resource removal of Grafana/Loki. Review the additional non-VPN service and account model requirements before implementation.

Define metrics collection/storage separately, evaluating a compatible backend such as Prometheus. Define Loki collection, labels, retention, storage limits, dashboards, alerts and access control; exclude secrets and user traffic payloads from telemetry. Evaluate Agent–Driver streaming with reconnection/reconciliation. Critical events and operation results require durable delivery and deduplication; sampled metrics have different requirements.

Acceptance: disposable lifecycle checks, real metrics/log ingestion and query, restart/recovery, retention limits and coexistence with VPN services.

## Stable 1.0.0 acceptance

Agree the feature cut after beta feedback; correctness, security and release validation remain gates regardless of which improvements are included.

Verify clean installation and supported update on disposable hosts, local/SSH node setup and removal, backup restore, profile revocation and temporary expiry. Check notifications, alerts and update discovery on real installations. Verify final artifacts and channel isolation.

Accept Workstation behavior on Linux, macOS and Windows: non-admin local use, PATH, SSH/session reuse, clipboard, focus, scrolling and resize. Check Telegram on desktop, Android and iOS and finish Quick Start acceptance. Record untested cases and current runtime limitations explicitly.

## After 1.0.0

Later scope depends on demand. Relative priority between Vaultwarden and a client SDK remains open; neither expands the initial refactor into a public plugin platform.

- **Vaultwarden:** follow [the service design](design/service-expansion.md). First review non-VPN lifecycle and account/profile requirements. Keep user vault data and service secrets separate from controller infrastructure secrets; bootstrap/recovery credentials must remain independently available. Require lifecycle, enrollment/revocation and backup/restore acceptance.
- **Workstation client SDK:** extract reusable SSH/trust/enrollment, backend requests, client workflows, operation observation/progress and structured errors. TUI is the first consumer; rendering/navigation/themes remain UI-owned and server authorization stays in Backend. Prove reuse with a headless example or second minimal client. Cross-language bindings are a separate decision.
- **New protocols/UI:** prioritize after existing VPN improvements based on feedback. Protocols use native adapters and common execution guarantees; additional UIs should reuse the client boundary.

## Deferred decisions

- **Custom roles and service modules:** evaluate demand for RBAC role creation and third-party services. Compare gradual internal adapters with a common module system used by built-ins and custom services. Public contracts, registries, generic UI builders and executable/Wasm plugin formats remain undecided. Existing authorization and security fixes remain core requirements; no paid-edition boundary is committed here.
- **Chain self-healing:** revisit only with demonstrated demand and proven manual chains; it does not block other roadmap work. Observe actual next-hop VPN usability separately from Agent RPC reachability. Use prepared allowed fallback routes/credentials during controller outages, journal versioned decisions and reconcile manual changes. Test intermittent failure, hysteresis/backoff, loops, exhausted routes, restarts and recovery. Preserve required hops and stop forwarding if permitted routes fail. Established-session continuity and entry-node failover require separate designs.
- **Journal retention:** define safe retention/compaction without losing recovery evidence or duplicate protection.
