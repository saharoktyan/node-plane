# Development plan

Updated: 2026-10-07. This is the main active development backlog. It replaces
the historical roadmap, migration/parity maps, Rich UI plans, protocol upgrade
plans and the implementation audit. Architecture and operator references are
separate documents, not competing task lists.

The next AWG/device and provisioning work package is specified in
[DEVICES_AND_PROVISIONING_PLAN.md](DEVICES_AND_PROVISIONING_PLAN.md). Follow its
delivery order: preserve existing peer identities before enabling additional
devices, then address port policies, defaults and access to future servers.

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

Four automatic reliability blocks are recorded below, using isolated PostgreSQL,
worker/driver fixtures and disposable protocol containers. Continue only the
remaining host/client-dependent and release/recovery contract items. Do not
require repeated user VPS reinstallations for cases reproduced automatically.

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

Implementation follow-up: config screens have collapsed QR/instructions and
URI blocks, followed by directly accessible configuration files.
AWG encoding uses compact JSON and maximum zlib compression with the same wire
format; user testing confirmed that standalone Rich code blocks still cannot be
copied in Telegram for iOS. The URI block now offers **Send link separately (iOS)**
directly below the monospace URI inside the same collapsed block,
which reads the currently authorized artifact and sends a regular Telegram
message with a code entity and Close button. This auxiliary message does not
replace the Rich control panel and is cleaned up on navigation. Actual iOS
clipboard behavior still requires user verification. The backup-size
fix is implemented and tested and included in the `0.4.3-alpha.39` release.

### Current quality-of-life work

- [x] Highlight forward wizard actions and action confirmations consistently.
  Start/create, Next, Skip, Review, Save and safe install/update confirmations use
  `primary`; destructive cleanup, restore and privilege changes retain `danger`.
  Back/Close remain neutral, and paired navigation rows are preserved. Explicit
  button styles carry through both Rich blocks and inline fallback. All 228
  Telegram workflow tests passed, including forward-row and update-confirmation
  style assertions; parameter-selection controls still highlight only selection.
- [x] Node creation starts with Local/SSH, followed by a small location template
  catalog (Latvia, Germany, Netherlands, Finland, United States and Singapore).
  Templates fill name, region, country flag and a fresh node key. SSH needs the
  SSH target and public address; Local needs only the public address. Protocol
  choices, review and manual custom creation remain available. Back preserves
  entered addresses and returns one step. Fresh key suffixes prevent reuse of
  retired identities; protocol defaults remain backend-generated. All 222
  Telegram tests passed, including template creation/back navigation and the
  regular-message URI action. The tests verify fresh artifact authorization,
  stale-delivery suppression and preservation of the main panel on Close.
- [ ] Device identities: issue independent VPN credentials per registered
  device, with optional platform metadata and individual revocation. Telegram's
  normal Bot API does not expose the user's OS; Mini Apps expose platform only
  after opening a web app. A requested device's OS also need not match the
  Telegram client used to obtain its config. Keep the regular-message URI action
  available even when platform-specific defaults are introduced. This is a
  future feature, not a prerequisite for the clipboard compatibility action.

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
- [x] Make registry-only node removal reliably accessible when VPS access is
  permanently lost, for example an expired hosting subscription. Telegram now
  provides a danger-styled action in Maintenance and unfinished/blocked removal
  screens, with confirmation naming the node and warning that downloaded tunnels
  may still work. Navigation invalidates confirmation; a stale callback requires
  confirmation again. The API serializes against the worker and rechecks current
  administrator privileges. Retirement removes all grants and list entries,
  supersedes unfinished work and blocks queued/running agent rollouts without
  inventing a successful remote result. The audit tombstone retains the reason
  and uncertainty; abandoned installation history no longer blocks restoration.
  Automated coverage includes a held worker lock, demoted administrators,
  pending/uncertain rollout retirement, stale config screens and restoring a
  post-retirement snapshot. All 76 PostgreSQL reliability tests passed on a
  disposable PostgreSQL 16 instance; no user VPS reinstallation was required.
  The final Python discovery run passed 692 tests with 86 environment-gated
  skips, including invalidation of confirmation on same-message navigation.
  Configuration restoration deliberately resets operational history, including
  retirement tombstones; it is not an archive of removal evidence.

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

Configuration links use a collapsed preformatted Rich block with **Send link
separately (iOS)** directly below the URI. QR/instructions remain collapsed and
files remain directly accessible. The separate message uses ordinary Telegram
code formatting; actual iPhone clipboard acceptance remains user-dependent.

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
The user confirmed on 2026-10-06 after v0.4.3-alpha.48 that both screens now
render as Rich UI on an installation with no nodes. The reported empty-list
fallback case is closed; diagnostics remain for future unrelated failures.

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

First automatic reliability block, 2026-10-06 (unreleased):
`tests/test_core_reliability_postgres.py` and `tests/test_backups_postgres.py`
pass 21 checks in a separate process using disposable PostgreSQL 16. Twenty
exercise real transactions; one launches a second worker process and confirms
that the shared process lock rejects it before database access. Production
hosts, agents and VPN runtimes were not touched.
Full Python discovery also passes: 692 tests, 22 skipped (including the real
PostgreSQL checks run separately above). Changes are not yet tagged/released.

Covered cases:

- Competing expected revisions and duplicate idempotency keys; two executors
  claiming the same task; in-flight work excluding another profile on that node.
- Grant versus node drain, and profile edit versus drain. Fixed the reversed
  profile/node lock order by taking the command guard before drain row locks.
- Injected termination before dispatch, after durable remote success, and
  result-transaction failure. Recovery confirms the driver journal once;
  absent/failed evidence remains blocked and prevents later conflicting work.
- Restore checksum/schema rejection, idempotent admission, concurrent command
  exclusion, failed revocation, partial transaction rollback and interrupted
  replacement followed by completion. The restoring administrator retains login.
- Restore with a saved node resource inventory. Fixed the missing transient
  inventory cleanup that previously caused a PostgreSQL foreign-key failure.
- Fixed restore exclusion inside the command transaction, closing the race
  after HTTP preflight. Backup policy writes use the same guard; a duplicate
  restore request can still retrieve its original job.

Run these integration modules separately from discovery: other modules install
database doubles. Set `NODE_PLANE_TEST_POSTGRES_DSN` to a disposable database
and `PYTHONPATH=app`, then run
`python -m unittest tests.test_core_reliability_postgres tests.test_backups_postgres -v`.
Each test creates and drops its own schema. Injected termination uses a
`BaseException` fault boundary; it does not prove OS kill, network partition or
real agent journal durability. Coordinated update rollback, traffic/delivery
races, full host ownership checks and protocol fault tests remain below.

Second automatic block, 2026-10-06 (unreleased): ten additional disposable
PostgreSQL checks cover coordinated controller rollback, failed rollback,
late health confirmation, unrelated-unit rejection, an interrupted launch,
agent-only partial failure with another node completing, queued cancellation,
fresh administrator authorization and recovery-route access behind the gate.
Shell fixtures execute the actual rollback function for failures in each of
backend/worker/driver/Telegram, with both healthy and unhealthy restored services;
they restore temporary binaries, units, environment and release symlink.
No host services were started/stopped by these fixtures.
Combined isolated modules pass 31 tests. Full Python discovery passes 704
tests with 32 skipped; PostgreSQL integrations are executed separately.

Updates now supports administrator-only cancellation before any dispatch and
read-only rechecking of a blocked coordinated core result. Both are available
on the Rich progress screen, in English and Russian, and use the worker lock.
A failed/unknown rollback or missing launch identity does not release the gate.
Rechecking never schedules another installation. A healthy durable outcome can
resume agent updates; failed agents remain a partial result and do not roll
back the confirmed controller. The local repair examples now use the installed
worker's actual lock path, `/opt/node-plane/shared/data/backend-worker.lock`.

Third automatic block, 2026-10-06 (unreleased):
`tests/test_policy_delivery_postgres.py` passes 30 additional checks using
disposable PostgreSQL 16 and paused/failing counter fixtures. All four isolated
reliability modules together pass 61 checks.
Full Python discovery passes 734 tests with 62 skipped; the PostgreSQL modules
are run separately rather than replaced with database doubles.

- Traffic collection drops samples when policy changes, grants are revoked,
  profiles are frozen/deleted, nodes drain, restore starts or ownership changes
  during the read. Disabling collection prevents agent calls and hides summaries;
  re-enabling establishes a new baseline without counting the disabled period.
- Counter timeouts preserve the baseline; epoch changes, monthly rollover,
  AWG counter identity and BIGINT overflow have explicit regression coverage.
  Ownership transfer is a guarded SQL fixture, not a new transfer API.
- Announcement preview/queue/claim excludes the sender, pending accounts,
  missing profiles and non-Telegram identities. Concurrent deletion and claims
  serialize; duplicate queue requests produce one delivery, and competing
  adapters cannot acquire the same recipient with different claim IDs.
- Fixed repeat-claim authorization: a lost-response retry now rechecks recipient
  eligibility, sender privileges and restore exclusion. Previously issued claims
  that lose eligibility become unknown instead of being returned or replayed.
  Restore retry coverage includes a synthetic stale claimed row; normal restore
  admission still excludes active delivery work.
- Fixed traffic-policy writes using cached administrator privileges: the account
  must still be approved and an administrator inside the guarded transaction.

Delivery authorization is checked at claim time. These tests do not cancel a
Telegram request already submitted after a claim, prove external exactly-once
delivery or exercise real network partitions. No messages were sent and no VPS
was modified. Real protocol counters and host ownership/removal remain separate
checks. Run the integration modules separately from normal discovery:
`python -m unittest tests.test_core_reliability_postgres tests.test_backups_postgres tests.test_update_recovery_postgres tests.test_policy_delivery_postgres -v`
with a disposable `NODE_PLANE_TEST_POSTGRES_DSN` and `PYTHONPATH=app`.

- [ ] Add a dedicated **Diagnostics & Recovery** menu in the Telegram admin UI.
  Deferred on 2026-10-06 and activated with workstation recovery on 2026-10-07. List
  blocked operations by node/profile/controller, explain the cause and show
  applicable actions: queued cancellation, safe retry, journal confirmation,
  audited retirement after agent restart, and explicit registry-only removal
  for permanently inaccessible VPSs. Reuse backend repair contracts instead
  of deleting fences or journals. Surface actions currently available only in
  trusted local admin commands. Include an emergency controller recovery path
  for missing launch identity and failed rollback: quiesce the old update
  process and establish the repaired stack state before releasing its gate.
  Preserve uncertainty and audit history; never present force-unlock as success.

  Workstation-first implementation started on 2026-10-07. The embedded
  `scripts/installation_diagnostics.py` returns versioned secret-free observations
  for files/configuration presence, release, disk, PostgreSQL/maintenance,
  controller systemd units, worker/API readiness and blocked operation counts.
  TUI diagnostics include an opt-in selector for per-unit confirmed recovery:
  start installed inactive/failed controller units, recheck maintenance under the
  existing account guard, and rerun observations. No gate/journal removal or
  replay is performed. Unit fixtures cover maintenance/missing guard/active unit
  refusal and allowlisted dispatch. Subsequent operation recovery and Telegram
  integration are described below; live VPS acceptance is not yet established.

Follow-up: the read-only `/api/v1/system/recovery` inventory now paginates
  unfinished updates, node jobs, agent rollouts, profile tasks, removals and
  backups without exporting intent/result payloads. TUI diagnostics use it when
  the API is healthy; recovery mode confirms applicable existing cancel/recheck
  actions and retains the exact operation identity after an unconfirmed result.
  Telegram Settings now exposes a RU/EN Rich recovery inventory and confirmations
  for existing update cancel/recheck and node journal resolution routes. Missing
  evidence never becomes a force unlock. Additional operation-specific repairs,
  controller launch/rollback emergencies and Telegram host diagnostics remain
unfinished. The original full recovery-menu checkbox therefore stays open.

Workstation account binding (2026-10-08): a permanent controller-side key fingerprint
binding identifies the same backend administrator across sessions. Initial
registration requires an explicit administrator selection; installation registers
the key after controller verification. Conflicting selectors, revoked keys and
deleted/disabled/demoted administrators fail closed. Privileged SSH revocation
and restoration are audited; restoration does not revive old bearer tokens. The
binding reuses backend account UUIDs and leaves future non-SSH user interfaces
free to use their own authentication methods. The workstation Nodes browser is
implemented; its live VPS acceptance remains a separate check.
Telegram audit is now a compact event list with native expandable timestamp/actor
rows, short action/outcome summaries, and full identifiers inside the details.
Telegram structural navigation (2026-10-08): member and admin panels now render
owner-bound link-style buttons inside a compact breadcrumb paragraph. The current
screen is bold plain text. Long paths drop the leftmost ancestors and shorten
long labels, with an ellipsis opening the full parent list in the same message.
Returning restores the panel snapshot without replaying any backend operation.
Server/profile labels reuse already loaded records; breadcrumbs add no backend
reads. Existing list cursors/search and server drafts are retained. Leaving a
changed access editor or unfinished node wizard through an ancestor requires
discard confirmation. Notifications keep their own FSM and navigation snapshot.
Cards containing configuration files/photos keep a full wrapping path, avoiding
upload snapshots and preserving their media. Plain fallback exposes the same
parent destinations as inline buttons. Telegram client visual acceptance remains
to be checked on Android, iOS and Desktop.

Workstation attribution (2026-10-07): credentials bind the selected admin name,
SSH username and workstation key fingerprint. Secret-free API admission/HTTP
completion events are available in a paginated Rich audit viewer. Installer and
offline diagnostic/recovery steps use the host journal. Account deletion and
configuration restore retain the backend audit history. Root SSH is the trust
boundary, not an independent identity proof. SSH key enrollment now has host
admission/completion records for both workstation and controller keys. Controller
key preparation also records the selected admin, target, key fingerprint and
verified/unconfirmed outcome in backend audit, sharing the target's operation ID.
Follow-up: linking asynchronous terminal job outcomes directly into the audit viewer.

- [x] Test concurrent worker claims, expected-revision conflicts and competing
  mutations, including traffic-policy changes during collection and recipient
  eligibility changes before delivery claims. See the isolated PostgreSQL
  evidence above and below; external sends already submitted are outside this
  transaction boundary.
- [x] Test crashes and timeouts before dispatch, after remote mutation and before
  result persistence. Unknown outcomes must remain inspectable/blocked rather
  than trigger blind automatic replay. Actual SIGKILL, disconnected gRPC and
  durable helper journal coverage is recorded below; external host scheduling
  and long-lived network partitions remain separate acceptance boundaries.
- [ ] Test core update failure and rollback, rollback failure, and agent-only
  partial failure. Verify progress survives leaving the screen and restarts.
- [ ] Restore a real PostgreSQL configuration snapshot; verify scope, checksum,
  schema validation and maintenance exclusion. Document that configuration
  snapshots are not full host, VPN runtime or secret disaster-recovery backups.
- [ ] Test full node/controller removal with owned configs, credentials, agent
  binary/unit/process and bindings; preserve unrelated resources. Include
  inaccessible nodes and partial cleanup/recovery.
- [x] Verify queued announcements recheck recipient eligibility and exclude
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

Fourth automatic block, 2026-10-06 (unreleased): process faults, removal and
actual protocol runtimes are now covered by three additional modules:

- `tests/test_process_faults_postgres.py`: five checks use real SIGKILL before
  dispatch, inside an agent-helper mutation, after the durable helper journal
  commit and inside a PostgreSQL result transaction. They verify flock release,
  transaction rollback, blocked uncertainty and read-only success recovery.
  A gRPC server process is killed after mutation but before replying; a restarted
  server confirms the exact journal entry without a second mutation. Server and
  mutation effects are fixtures; the production durable helper is used.
- `tests/test_removal_saga_postgres.py`: six checks cover unprovisioned retirement,
  pending grants revoked before cleanup, preservation of other nodes/grants,
  registry-only retirement of blocked work, failed final host verification, and
  controller reset/removal. Real PostgreSQL transactions preserve unrelated
  application tables, keep the operator on reset and require shutdown acknowledgment
  before full removal. Driver and host outcomes are explicit fixtures.
- `tests/test_runtime_docker.py`: nine opt-in checks use the production Xray
  26.3.27 and pinned AWG 3.1 images in isolated namespaces. Xray API add/revoke
  does not restart the process; actual restarts retain additions and revocations.
  Injected second-inbound API failures retain durable, repairable state. This
  tests live HandlerService users, not REALITY/TLS handshakes or client behavior.
  AWG quic/dns/chaos presets carry small packets, 1100-byte payloads at MTU 1280
  and short parallel bursts without loss. One-second keepalive, peer revocation
  and restoration with unchanged credentials are exercised. These are local
  functional checks, not Internet PMTU, long-idle/NAT or capacity benchmarks.
  Real Docker inventory/removal deletes owned current/previous containers and
  refuses an unrelated mount before any deletion. The actual agent uninstall
  shell and generated controller uninstall shell execute in disposable containers,
  checking processes, units, binaries, journals/archives, credentials, exact SSH
  key removal and preservation of unrelated resources. Process-backed systemctl
  fixtures model service stops; real systemd-run scheduling is not tested here.

Fixed a reproduced native-agent cleanup defect: custom config locations could
retain exact `.bak`/settings backups and interrupted `.xray-config-*` or
`.xray-user-*` files. Agent cleanup now removes these individual artifacts and
the independent inventory includes temporary Xray files. Legacy `.dirbak.*`
directories are captured for verification; directories outside the managed root
are not recursively erased, so a remaining directory prevents verified retirement.
Arbitrary historical artifacts outside known locations still require inspection.

All six isolated PostgreSQL reliability modules pass 72 checks. The Docker module
passes nine checks; native Rust suites pass 12 agent and 33 driver tests. Full
Python discovery passes 754 tests with 82 skipped, including opt-in integration
checks executed separately. Product Docker images were neither built nor published;
production hosts and actual Telegram delivery were not used. Disposable containers,
networks and test schemas are cleaned up after the run.

Run PostgreSQL integrations separately with a disposable
`NODE_PLANE_TEST_POSTGRES_DSN` and `PYTHONPATH=app`:
`python -m unittest tests.test_core_reliability_postgres tests.test_backups_postgres tests.test_update_recovery_postgres tests.test_policy_delivery_postgres tests.test_process_faults_postgres tests.test_removal_saga_postgres -v`.
Run runtimes only on a disposable Docker host with the referenced Xray/AWG images,
`postgres:16-alpine` and the existing `node-plane-release-builder:bookworm` image
cached: `NODE_PLANE_TEST_DOCKER=1 PYTHONPATH=app python -m unittest tests.test_runtime_docker -v`.
AWG fixtures grant NET_ADMIN only inside their container namespaces; they do not
use host networking or publish VPN ports. Normal discovery skips these fixtures.

### Release and recovery contract

- [x] Document supported backend/client/driver/agent combinations and rejection
  of incompatible mixes; API/protobuf versioning alone is not a compatibility
  guarantee. RELEASES.md specifies a coherent release commit, temporary rollout
  mixes and actual enforcement boundaries; a universal startup version handshake
  and arbitrary mixed-version support are explicitly deferred.
- [x] Define schema downgrade limits, manual recovery, certificate renewal and
  CA rotation/revocation procedures. RELEASES.md distinguishes version policy
  from schema compatibility, complete-stack recovery from the partial rollback
  helper, and rollout-triggered leaf renewal from deliberate trust rotation.
  Fine-grained revocation/automatic CA rotation are deferred; actual trust-rotation
  acceptance remains a disposable-host check, not claimed from documentation.
- [x] Update the release checklist for the complete systemd stack, clean and
  upgraded hosts, release asset checksums and Debian-compatible binary ABI.
  RELEASES.md follows the existing tag/build/publish scripts and glibc 2.36 gate.
  Record unpublished cleanup/backup changes in release notes when released;
  writing this checklist does not claim a release or new host acceptance occurred.

Closure means these tasks are completed or explicitly scoped/deferred with a
reason. It does not require implementing every historical idea or every upstream
protocol option. Future feature proposals stay below, outside this closure gate.

## 2. Next: workstation installation assistant

Build a native Rust CLI/TUI, as selected on 2026-10-06. It runs on the user's
local Linux or Windows 10/11 computer without Python or an external SSH client,
and supports installing the controller, updating the complete stack, basic
diagnostics and preparing target nodes without routine interactive server shells.

Before implementation, decide supported workstation platforms, SSH authentication
methods and privilege elevation. Distinguish controller installation from node
enrollment. Bootstrap must work before a backend API exists; use the supported
installer and release tooling rather than duplicating orchestration policy.
Subsequent business actions use backend authorization and operation contracts.

Current implementation and remaining acceptance:

- [x] Add workstation Nodes: region folding, local name/region/code search,
  responsive pagination, compact persisted-state cards and explicit service
  inspection. Retain SSH preparation; expose confirmed agent/Docker/bootstrap
  jobs with private progress records, administrator-scoped lost-dispatch lookup
  and no uncertain replay. Server creation/editing and custom SSH-port agent
  setup remain in Telegram. Region navigation and keyboard/mouse rendering are
  tested in simulated terminals; real VPS acceptance remains manual.
- [x] Implement embedded SSH with explicit host trust and a workstation-owned
  Ed25519 key. First-password login appends its public key without replacing
  existing keys, then verifies an independent key login. Subsequent connections
  use the key. External key import/ssh-agent/MFA remain a separate increment.
- [x] Implement the install form, masked credentials, X/7 stage progress, bounded
  logs and failure reporting around the supported installer. Check host paths,
  prerequisites and final controller readiness. Marked same-target partial
  installs preserve generated configuration on an explicit retry.
- [x] Implement basic read-only installation checks and private local logs with
  submitted bot tokens/PostgreSQL URLs redacted.
- [ ] Validate a full assistant-driven install/retry on a disposable VPS and
  manually exercise Windows 10/11 terminals. Native Windows/Linux CI is added;
  a configured workflow is not evidence of a successful remote CI run.
- [x] Update through the coordinated stack flow and expose rollback/partial failure.
  Persist exact command identity before dispatch; reconcile a lost response by
  account and command, with detach/resume rather than uncertain replay.
- [x] Install the controller's public SSH key into a target node's authorized keys
  without replacing unrelated keys or copying private keys between machines.
  Repeated setup must be safe; initial SSH authentication and sufficient
  privileges are still required.
  Verify a real controller-to-node key login with the workstation-confirmed host
  pin before claiming success. Registry creation and agent installation stay in
  the bot; live end-to-end acceptance remains pending.

The first implementation is in `rust/node-plane-cli`; see its English README
for usage, platform limits and credential handling. It embeds the installer
progress adapter while exporting the selected tag's runtime. Product Docker
installation remains unsupported. No release/tag or production VPS operation
was performed as part of this implementation.

Initial local evidence (2026-10-06): 24 Rust tests pass, including disposable
loopback SSH enrollment/reuse and uncertain-result cases; Clippy is clean with
warnings denied. Twenty-nine related Python installer/shell regression checks
pass. A native Linux pseudo-terminal smoke test renders the form and exits
cleanly without connecting to a VPS. The Linux release binary builds locally;
Windows CI configuration and full live installation remain unverified.

Update/enrollment increment evidence (2026-10-07): 40 Rust tests pass, including
HTTP account/idempotency headers, lost response classification, exact intent/job
binding, distinct rollback/partial outcomes and regional TUI pagination. Sixty-five
Python tests cover the scoped account bridge, update API contract, controller
login/known-host merging, SSH identity defaults and installer progress. Clippy
passes with warnings denied. The Linux release builds; native pseudo-terminal
smoke checks open install/update/node-preparation forms and exit cleanly without
connecting to a host. These are isolated fixtures, not a live VPS update
or a Windows terminal acceptance run.

Update verification follow-up (2026-10-07): 41 Rust tests pass, including a
centered progress window with a single stage label and step counter. Twenty-one
targeted Python checks cover final progress rereading after systemd completion,
rechecking the existing job, and atomic preservation of the shared runtime
environment during updates. A live report exposed a stale completion snapshot
and a transient missing PostgreSQL DSN. Both paths are fixed locally; recovery
of the existing production job requires administrator approval. The assistant
does not repeat controller installation to recover an uncertain result.

Workstation UI follow-up (2026-10-07): the form now uses an action sidebar and
editable fields with mouse hit targets alongside keyboard navigation. Rectangular
confirmation buttons are shared by action, host trust and password dialogs;
modal hit targets isolate background controls. Update node statuses include the
agent/runtime phase to distinguish failures. A live follow-up confirmed the agent
commit was current while runtime identity remained `0.1.0 / unknown`; the driver
now falls back to its compiled release identity when environment metadata is
absent or unknown. A runtime bundle regression covers these version markers.
Validation: 45 CLI tests and 34 driver tests pass. CLI Clippy is clean with
warnings denied. A native Linux pseudo-terminal check clicks Continue and Cancel
and verifies mouse capture is restored on exit, without connecting to a VPS.
Windows mouse acceptance and installation of the driver fix remain pending.

Connection memory and navigation follow-up (2026-10-07): the active sidebar item
uses an outlined cyan border/text instead of a filled background. Settings is
anchored at the bottom and supports creating, editing and selecting private
nonsecret installation preferences. Known connection fields are omitted from
action forms. Preferences survive restart and can be seeded from existing update
records; switching controllers clears secrets, target-node fields and an explicit
operation identity belonging to a different SSH connection. Conflicting preference
writes are refused. Esc returns focus to the sidebar and then opens an exit
confirmation with Cancel selected. Active update observation detaches; other SSH
workflows retain their connection until completion after the TUI closes.
Validation: 52 Rust tests and CLI Clippy pass. A native Linux pseudo-terminal
check creates/saves/reuses a profile after restart, exercises mouse controls and
verifies sidebar focus and cancel/confirm exit, without connecting to a VPS.

Quick profile switching follow-up (2026-10-07): a persistent selector above
Settings cycles saved installations and New profile via keyboard or clickable
arrows. Re-selecting New profile or pressing Enter opens creation; all action
controls and submission are disabled until a saved profile is selected. New
profile selection survives restart, and switching clears connection-specific
secrets and target fields. Compact sidebars retain all four actions. Regression
checks cover disabled hit targets, wrapping/persistence, creation, compact layout
and focus transfer from the switcher to action controls.

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
