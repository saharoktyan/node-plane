# Development plan

Updated: 2026-10-05. This is the only active development backlog. It replaces
the historical roadmap, migration/parity maps, Rich UI plans, protocol upgrade
plans and the implementation audit. Architecture and operator references are
separate documents, not competing task lists.

## Baseline and completed work

The supported stack is PostgreSQL + standalone Python backend/worker + aiogram
Telegram client + Rust driver/node agents. Controller installation is systemd
only; node protocol containers and the Rust release build container remain.

Completed: backend ownership of accounts, authorization, desired state and
durable operations; UUID identities with optional Telegram bindings; explicit
execution intents; local/SSH agents and mTLS; AWG 3.1; persistent Xray users plus
HandlerService changes without routine restarts; current config issuance and
re-provisioning after reinstall; freeze/expiry/revocation; Rich RU/EN member and
admin screens; regional bulk access; announcements; coordinated updates;
configuration backups; scoped removal and unavailable-node registry removal.

The PTB runtime, handlers, old catalog, dependencies and obsolete controller
Docker entrypoint have been removed. The remaining Python suite passes 615 tests
in an environment without PTB. This is automated evidence, not live acceptance
of every failure scenario.

The user reports successful current-functionality testing with PostgreSQL.
Fresh SSH agent installation has not been repeated because other servers run
unrelated projects; it is provisionally accepted from earlier successful tests.
Do not reopen the completed migration or demand unrelated servers for testing.

## 1. Close the current core cycle

Work through these items before starting the workstation assistant. Update this
file with implementation and verification results as each item is closed.

### Small implementation gaps

- [x] Replace manual local-agent TOML parsing in the Rust driver with a real
  parser. Verify comments, whitespace, quoted values, malformed files and
  precedence of explicit agent targets over discovered local targets.
- [x] Change traffic accounting to an administrator-controlled installation
  policy. The global switch disables collection and all traffic-dependent
  features when off; when on, collect statistics for all profiles without a
  member opt-in. Remove member consent controls and consent gates consistently
  from collectors, APIs and UI; migrate existing preferences and test both
  transitions. Show members whether accounting is enabled. Implemented with obsolete preference cleanup and global generation fencing.
- [x] Add an authorized administrator profile traffic read and compact UI under
  that global policy. Report monthly totals and per-node/protocol usage, and
  distinguish unavailable from zero. Totals appear on the admin profile card;
  the server breakdown is a separate regional paginated screen. Protocol rows
  are compact bullet-separated text. Future limits must use the same policy.
- [x] Include the existing backup-size fix in `0.4.3-alpha.39`: small backups must display B/KiB
  instead of rounding to `0.0 MiB`.

Implementation follow-up: config screens use a Show/Hide URI action, with
QR/instructions above it and directly accessible files below it. Code and files
are no longer nested in details blocks, avoiding the user-reported iOS issue.
AWG encoding uses compact JSON and maximum zlib compression with the same wire
format; real iOS interaction still requires user verification. The backup-size
fix is implemented and tested and included in the `0.4.3-alpha.39` release.

### Execution and ownership cleanup

- [ ] Inventory retained legacy Rust RPCs and PostgreSQL business-table paths.
  Trace callers before removing unused paths; keep backend-owned policy and
  explicit intents as the sole normal mutation contract. Update protobufs,
  generated stubs and component references together where removal is safe.
- [ ] Review orphan-runtime cleanup and controller-owned resource inventories.
  Define the supported recovery path without deleting unrelated services,
  packages, containers or credentials. Registry-only removal cannot guarantee
  remote deletion.
- [ ] Document operation/artifact retention, journal growth and cleanup rules.
  Keep node-agent SQLite command journals; their replacement is not planned.
  Never remove duplicate-protection records while commands can still be replayed.

### Focused integration evidence

Use isolated PostgreSQL databases, local fixtures and disposable nodes. Record
the tested release and result; SQL fakes do not prove PostgreSQL lock semantics.

- [ ] Test concurrent worker claims, expected-revision conflicts and competing
  mutations, including traffic-policy changes during collection and recipient eligibility
  changes during delivery.
- [ ] Test crashes and timeouts before dispatch, after remote mutation and before
  result persistence. Unknown outcomes must remain inspectable/blocked rather
  than trigger blind automatic replay.
- [ ] Test core update failure and rollback, rollback failure, and agent-only
  partial failure. Verify progress survives leaving the screen and restarts.
- [ ] Restore a real PostgreSQL configuration snapshot; verify scope, checksum,
  schema validation and maintenance exclusion. Document that configuration
  snapshots are not full host, VPN runtime or secret disaster-recovery backups.
- [ ] Test full node/controller removal with owned configs, credentials, agent
  binary/unit/process and bindings; preserve unrelated resources. Include
  inaccessible nodes and partial cleanup/recovery.
- [ ] Verify queued announcements recheck recipient eligibility and exclude
  the sender and deleted profiles.
- [ ] Extend protocol checks where evidence is missing: Xray API user persistence
  across restart and partial API failure; AWG idle/keepalive, MTU/large packets,
  load and each CPS preset. Check profile identity restoration and failed
  settings/reinstall rollback without issuing stale artifacts.
- [ ] Record Rich fallback/notification/artifact behavior on Telegram Android,
  iOS and Desktop. Retain plain fallback and one-control-message navigation.

Fresh clean/partial/failed SSH bootstrap can be retested when a disposable host
becomes available. This is an explicit environment-dependent validation item,
not evidence of a known SSH defect. TCP troubleshooting specific to iOS/v2rayBox
is deferred: the same config works on Android and Linux NekoBox.

### Release and recovery contract

- [ ] Document supported backend/client/driver/agent combinations and rejection
  of incompatible mixes; API/protobuf versioning alone is not a compatibility
  guarantee.
- [ ] Define schema downgrade limits, manual recovery, certificate renewal and
  CA rotation/revocation procedures. Automatic CA rotation is not assumed.
- [ ] Update the release checklist for the complete systemd stack, clean and
  upgraded hosts, release asset checksums and Debian-compatible binary ABI.
  Record unpublished cleanup/backup changes in release notes when released.

Closure means these tasks are completed or explicitly scoped/deferred with a
reason. It does not require implementing every historical idea or every upstream
protocol option. Future feature proposals stay below, outside this closure gate.

## 2. Next: workstation installation assistant

Build a small CLI first; a TUI is optional. It runs on the user's local computer
and supports installing the controller, updating the complete stack, basic
diagnostics and preparing target nodes without routine interactive server shells.

Before implementation, decide supported workstation platforms, SSH authentication
methods and privilege elevation. Distinguish controller installation from node
enrollment. Bootstrap must work before a backend API exists; use the supported
installer and release tooling rather than duplicating orchestration policy.
Subsequent business actions use backend authorization and operation contracts.

Acceptance for the first slice:

- Connect with existing SSH credentials and verify the host identity.
- Check prerequisites, install the systemd stack and report understandable
  progress and next steps. Resume safely after a partial installation.
- Update through the coordinated stack flow and expose rollback/partial failure.
- Collect useful diagnostics with credentials and config secrets redacted.
- Install the controller's public SSH key into a target node's authorized keys
  without replacing unrelated keys or copying private keys between machines.
  Repeated setup must be safe; initial SSH authentication and sufficient
  privileges are still required.

## 3. Preserved ideas, outside the current delivery gate

| Idea | Preconditions and scope |
| --- | --- |
| Device identities and independent revocation | Separate credentials/peers per device; define identity and UX before quotas. |
| Authenticated VLESS subscriptions | Current access/config generation, credential expiry and revocation; no stale cached subscriptions. |
| Traffic/device limits | Explicit enforcement and reliable accounting; sampled statistics are not a billing guarantee. |
| Richer observations, fleet diagnostics and bulk maintenance | Extend the existing backend; preserve per-node outcomes and authorization. |
| Browser/mobile/general CLI clients | Separate credentials and documented API compatibility; no Telegram-dependent identity. |
| Identity-only user importer | Add after schema stabilizes; do not resurrect obsolete nodes, grants or runtime secrets. |
| Matrix/password-manager or new VPN service | One complete curated adapter with onboarding, revocation, update, backup, restore and removal. No generic Telegram workload scheduler. |
| Live progress streaming and remote cancellation | Durable remote outcomes and recovery semantics first; polling remains supported. |
| Optional separately distributed Pro modules | Deferred indefinitely. If revived, enforce capabilities in backend, distribute missing implementation separately, and keep the free core independent of licensing availability. Define offline/expiry behavior and package compatibility; local code cannot guarantee protection against a server owner. |

PTB v22 migration, inprocess execution, old command aliases, a second copy of
the product for Pro and controller Docker installation are superseded decisions,
not remaining tasks. Current supported commands are `/start`, `/help`, `/id`,
`/version` and `/status`.

## Reference documents

- [Core execution boundaries](CORE_ARCHITECTURE.md)
- [Backend API and authorization](BACKEND_API_CONTRACT.md), with current routes
  and implementation details in [backend reference](app/backend/README.md)
- [Curated service expansion boundaries](SERVICE_EXPANSION_ARCHITECTURE.md)
- [Protocol configuration reference](PROTOCOL_REFERENCE.md)
- [Telegram client and presentation](app/telegram_client/README.md)
- [Installation](INSTALL.md), [operator reference](BOT_REFERENCE.md) and
  [release checklist](RELEASE_CHECKLIST.md)

The consolidated decisions replace the former ROADMAP, CORE_PRO_PLAN,
DRIVER_MIGRATION_REMAINING, BACKEND_TELEGRAM_MIGRATION_PLAN,
PTB_AIOGRAM_PARITY_MAP, ADMIN_RICH_MESSAGE_PLAN, TMP_UPDATES_PLAN,
DRIVER_ARCHITECTURE, NODE_AGENT_ARCHITECTURE, CODE_REVIEW and
PLAN_IMPLEMENTATION_AUDIT documents. Protocol upgrade research is retained as
configuration reference rather than an unfinished migration plan. Historical
implementation details remain available in Git history.
