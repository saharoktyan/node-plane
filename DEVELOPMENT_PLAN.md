# Development plan

Updated: 2026-10-06. This is the only active development backlog. It replaces
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
Fresh SSH agent installation and full SSH removal passed user testing on
2026-10-06; local full removal passed earlier clean-VPS testing.
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
  transitions. Member settings do not show this admin-only policy; enabled
  accounting is visible through profile statistics. Implemented with obsolete
  preference cleanup and global generation fencing.
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

Protocol-only cleanup now verifies the configuration bind mount before deleting
containers, plans every candidate before mutation, removes by immutable ID and
includes matching `-previous-<pid>` leftovers. Agent decommissioning now uses
the same mount checks, previous-container discovery and immutable IDs, preserves
graceful AWG shutdown and refuses to erase files if Docker inventory cannot be
read or managed containers remain. Regression tests cover unrelated mounts and
malformed inventory. Independent verification now persists a pre-drain inventory
of configured paths, current backups and container names, ties it to the host
fingerprint, and validates the digest of that same inventory before retirement.
Shell-level tests verify custom config leftovers and previous-container names.

- [x] Inventory and remove legacy Rust driver RPCs and PostgreSQL business-table
  paths. Removed TelemetryService, legacy NodeService methods and old
  ProvisioningService/RuntimeService methods together with protobuf messages,
  generated clients and unused transport helpers. The driver no longer depends
  on `tokio-postgres`, database credentials, registry rows or legacy profile
  tables. Supported backend intents, inspection, recovery, decommissioning and
  operation lookup remain. Agent-side maintenance guards and SQLite journals
  are retained; their presence does not reintroduce driver business policy.
- [ ] Review orphan-runtime cleanup and controller-owned resource inventories.
  Define the supported recovery path without deleting unrelated services,
  packages, containers or credentials. Registry-only removal cannot guarantee
  remote deletion.
  Inventory and recovery boundaries are recorded in CORE_ARCHITECTURE.md.
  Implemented preflight validation before any runtime cleanup, refusal of
  recursive deletion outside the managed root/symlinked paths, and non-forced
  image cleanup without deleting global historical tags. Both protocol-only
  cleanup and agent decommissioning verify container mounts and include previous
  containers. Independent final verification now checks captured custom paths
  and container names. Remaining: disposable-node evidence for the complete
  removal saga, historical artifacts outside the captured locations, and backup
  artifacts created outside the runtime root after the inventory was captured.
  Alpha.43 manual testing found local removal blocked at `preparing` while the
  agent remains active. Diagnosis confirmed a commands-only bootstrap journal
  and historical successful `ensure` fences absent from the new backend's DB.
  Preparation now accepts a valid commands-only journal and durably revokes all
  remaining historical targets before fencing cleanup. Interrupted commands
  still block removal; failed historical revocation is not replayed blindly.
  The old generic error incorrectly described every cleanup failure as
  connectivity loss. Worker phase/error logging and
  distinct cleanup/verification messages have been added. A separate later-step
  defect was fixed: systemd agent uninstall must not require HOME. Explicitly
  rejected uninstall RPCs remain retryable; ambiguous transport failures remain
  fenced. Do not require a fresh installation to investigate an existing blocked
  operation or claim full-removal acceptance from unit tests alone.
  Agent onboarding now binds command journals to the controller's persistent
  database UUID. New-controller onboarding quiesces the previous agent and
  archives SQLite files, sidecars and removal fences with a manifest, reporting
  the archive path in installer output and the Telegram rollout result. Same-
  controller updates retain active journals; legacy adoption uses the installed
  CA as conservative ownership evidence. Actual SSH archival still needs a
  disposable-node acceptance run.
  Clean local installations can now decommission without generating an SSH key;
  host identity, inventory, service and container verification remain mandatory.
  The rollout success screen returns to the admin menu, and unbootstrapped nodes
  have a separate status. Local clean-VPS removal passed manual acceptance on
  v0.4.3-alpha.45; SSH removal passed user testing on 2026-10-06.
- [x] Document operation/artifact retention, journal growth and cleanup rules.
  Keep node-agent SQLite command journals; their replacement is not planned.
  Never remove duplicate-protection records while commands can still be replayed.
  CORE_ARCHITECTURE.md records actual expiry versus physical retention, journal
  growth, backup/release rules and the prerequisites for future payload pruning.

SSH installation passed manual testing on 2026-10-06. Removal was blocked before
draining because it required a separately configured verification key. The
worker now automatically provisions an independent temporary key, verifies the
host before draining, and removes the temporary authorized-key entry after the
final artifact check. Configured independent keys remain supported. Automated
coverage includes credential reuse, failed-check fencing and preservation of
unrelated authorized keys. The user subsequently confirmed successful SSH
removal on 2026-10-06; this does not independently verify every historical
artifact or failure scenario.
Node lists and cards expose deletion progress and blocked deletion separately,
with a direct entry back into the removal screen.

Configuration links now use a top-level preformatted Rich block instead of
inline monowidth text. The show/hide button, collapsed QR and visible files
remain unchanged. This is an iOS copying workaround candidate; copying the full
AWG/VLESS URI on an actual iPhone still requires manual acceptance.

Rich fallback investigation on 2026-10-06: the user reported repeatable plain
rendering for update progress and the saved notification profile overview.
Existing logs do not expose failed Rich edits, so the Telegram rejection is not
yet confirmed. These screens could emit empty details blocks for no server
items or no grants. Update progress now omits the empty server list; profile
overview shows a localized no-access message; the shared builder suppresses
empty details blocks. Edit/send/notice failures now log a sanitized category
without message contents or credentials. Regression tests cover both empty
screens and recovery from a per-message fallback on the next render. Live
acceptance and the actual Telegram error remain outstanding.

Registry entries with no applied runtime, rollout attempts, mutation jobs,
settings tasks, profile commands/grants or bound cleanup target can now be
removed without contacting a nonexistent agent. This is explicitly metadata-only
retirement, not verified host cleanup; even a failed rollout prevents the fast
path. Blocked revocations now stop the removal saga with an explicit attention
state instead of showing a pending count indefinitely. The latest SSH setup
failure was reported alongside a missing bot authorized-key entry; installation
was resolved after restoring that key; the user confirmed SSH removal works.
Regression coverage also verifies removal with both protocol grants queued and
with one already applied: remaining ensures are superseded, both targets are
revoked, and runtime cleanup waits for confirmation. The focused lifecycle,
removal and executor suite passes 38 tests; this is automated fixture evidence.

### Focused integration evidence

Manual result reported on 2026-10-05, v0.4.3-alpha.45, clean local VPS:
full removal completed without errors; the node disappeared from every profile;
returning to an old screen did not allow new configuration retrieval; protocol
containers were stopped and the agent systemd service was removed. This report
does not independently establish absence of every binary, volume or historical
artifact. SSH installation/removal also passed user testing on 2026-10-06. Continue the
remaining failure, ownership and integration checks without repeating clean
installation tests unnecessarily.

Manual acceptance checklist (use disposable nodes; apply to the current release):

1. Update to the tagged release through Updates. Core components become current;
   reachable agents update, and failed agents are listed as partial failures.
2. Open member/admin profile cards on a fresh installation with traffic collection
   enabled and no samples. Both cards load without PostgreSQL datatype errors.
3. Install local agent and both protocols, grant two profiles different protocol
   combinations, save working configs, then remove the node. All grants disappear,
   issued tunnels stop, and unrelated nodes/grants keep working. Refresh member
   Profile/Get config and the admin grant editor; the removed node must be absent.
4. Keep an old config screen open during removal. Its buttons must not issue a
   stale config or recreate the node/access after removal.
5. Remove an installed node with no profile grants, then an agent-only node without
   protocol runtime. Both must complete without requiring nonexistent configs.
6. Leave the removal screen before completion and revisit it. Backend workers
   continue; refreshing does not launch another destructive operation.
7. Stop the agent before removal. Full removal cannot claim success. Registry-only
   removal removes node/grants from the bot and explicitly leaves remote state
   unverified. It cannot promise that downloaded configs stopped working remotely.
8. Interrupt agent connectivity during revocation. Runtime cleanup must wait for
   revocations; uncertain outcomes require recovery, not a blind repeat.
9. After successful full removal, independently check agent unit/process/binary,
   runtime/config/journal/state/log paths, current and previous containers, and the
   exact bot authorized-key entry. Controller services, Docker and unrelated
   authorized keys/files/containers must remain.
10. Create a stopped `<xray-name>-previous-<pid>` fixture with the same config bind
    mount on a disposable node. Full removal must also delete this leftover.
11. On a separate disposable node with no grants, substitute an unrelated container
    under the configured protocol name. Ownership validation must prevent deletion
    of that container and runtime files; no successful retirement is allowed.
12. Confirm the retired node key cannot be reused: permanent retirement fencing
    rejects it. Create a replacement under a new key; only newly granted profiles
    get access, and old grants/configs must not return automatically.
13. Repeat full SSH removal with only the normal bot key and a pinned host key.
    The worker must prepare and verify its temporary identity before revocation,
    remove both bot and temporary keys at completion, and retain unrelated keys.
    A separate configured verification key remains an optional override. Failed
    preparation must block before revocation. Never use production nodes for
    ownership fixtures or connectivity interruption.

Configured-path capture, malformed inventories, shell-value injection rejection
and literal custom container-name matching also have automated regression tests.
Record actual node results separately; unit fixtures do not prove end-to-end
host uninstall or PostgreSQL concurrency behavior.

Use isolated PostgreSQL databases, local fixtures and disposable nodes. Record
the tested release and result; SQL fakes do not prove PostgreSQL lock semantics.

Unreleased regression evidence: two tests on disposable PostgreSQL 16 verify
fresh profile traffic summaries and nullable-owner isolation. Fixed the `42P18`
untyped-null-parameter failure affecting both member/admin profile summaries.
This does not close the concurrency, restore or node-removal integration items.

Restore admission regression fixed on 2026-10-06: historical blocked agent
rollouts for a node already removed with verified host cleanup incorrectly
caused `maintenance_busy`. Those audit records no longer block restoration;
pending/running rollouts, failed rollouts for existing nodes and unverified
retirements still do. The user confirmed all three blocking attempts belonged
to the verified retirement of `spb1`. A real disposable PostgreSQL 16 test
restores a configuration snapshot with that history and verifies profile data
and administrator login preservation. RU/EN backup screens now distinguish
admission refusal from failed revocation. Worker failure logs record phase,
sanitized error code/type and SQLSTATE without backup contents. This evidence
does not close restore validation, revocation failures or concurrency coverage.
The user confirmed successful backup restoration on v0.4.3-alpha.47 on
2026-10-06. The reported admission defect and ordinary restore path are closed;
failure, checksum, incompatible-schema and maintenance-concurrency checks remain
separate validation items.

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
