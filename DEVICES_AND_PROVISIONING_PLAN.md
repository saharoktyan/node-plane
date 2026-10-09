# Devices and provisioning plan

Created: 2026-10-07.

This document specifies the next work package referenced by
[DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md). It covers AWG devices, configuration
delivery, protocol provisioning defaults and access to future servers. It does
not reopen the completed PTB migration or workstation implementation.

## 1. Device identities and AWG peers

Status: implemented. On 2026-10-09 the user confirmed simultaneous AWG
connections using configurations for different registered devices.

A profile owns named devices, such as Phone, Laptop and Router. A device stores
an opaque UUID, a display name and lifecycle state. Do not request or infer an
operating system, hardware fingerprint or Telegram client platform.

Names are arbitrary (1–64 characters) and unique within a profile after Unicode
normalization, case folding and whitespace normalization. Different profiles
can reuse names. A deleting device reserves its name until retirement is
confirmed; renaming keeps the existing runtime identity and keys.

Each active device receives its own AWG peer on every eligible server. The
identity is `(profile, device, node)`: peers have independent key pairs and
tunnel addresses. Server obfuscation parameters remain common to the node.
Repeated issuance reuses the same peer; renaming a device changes only its
label. A device cannot grant access beyond its parent profile.

Adopt an existing profile-level AWG identity as the first device, named Device 1,
without changing its runtime name, keys or tunnel address. Do not rotate working
configurations during migration. The first AWG grant for a profile without any
device history may create this default device. Deleting the last device must
not silently create a replacement.

Device deletion immediately prevents new configuration issuance and queues
durable revocations on every current or historical target. Keep a tombstone
until all remote outcomes are confirmed. Freeze, expiry, profile deletion and
node removal must revoke every device, including pending ensures. Uncertain
remote mutations retain their operation identities and require recovery.

Implement device work through the existing worker/outbox and revision fences;
do not add an independent executor that bypasses node maintenance locks.
Extend task identity and all latest-task/revocation queries to distinguish
devices. The existing runtime-name-based agent RPC can provision distinct peers.

Traffic collection, when enabled by the administrator, needs independent peer
counter baselines before aggregation into profile/node/protocol/month totals.
Removing one device or resetting its counters must not reset another device's
usage. Devices and stable runtime identities belong in configuration backups;
command journals and issued artifacts remain excluded. Preserve compatibility
with pre-device backups through an explicit, validated conversion.

Initial delivery is AWG device isolation. VLESS remains profile-based until a
separate identity migration is justified; do not imply per-device VLESS
revocation while multiple devices share its credentials.

### Member interface

- Add a Devices screen with create, rename and confirmed delete actions.
- Configuration navigation: server → protocol → device → configuration.
  Skip the device picker when exactly one active device exists.
- Show an empty-state action when no active devices remain.
- AWG configuration labels:
  `{server_name} AmneziaWG · {username} · {device_name}`.
- Preserve region grouping, pagination, heading sizes, RU/EN translations,
  one-message navigation and existing button styles.
- Explain that deleting a device disconnects that device on all eligible
  servers; show operation progress and actionable failure states.

### Acceptance checks

- Repeated issuance, retries and duplicate commands produce one peer per
  device/node; different devices use different keys and addresses.
- Existing AWG configurations survive migration and renaming.
- Foreign, pending, frozen, expired and deleted-profile callers cannot issue
  device configurations or manage another profile's devices.
- Device deletion, parent grant revocation and node removal cover both
  dispatched peers and queued ensures; stale config callbacks are denied.
- Crash/restart/recovery preserves exact command identities and does not
  replay uncertain actions.
- PostgreSQL migration, backup restore and per-device traffic aggregation
  receive automated coverage. Concurrent connections from real AWG clients
  using different device configurations passed user acceptance on 2026-10-09.

## 2. iOS-compatible configuration delivery

Keep the monospace URI in the collapsed Rich block. Immediately below it, in
the same block, retain **Send link separately (iOS)**. That action reauthorizes
the artifact and sends an ordinary Telegram message with a code entity and a
Close button, leaving the main Rich panel intact.

Rendering does not depend on a stored device OS: the device using the tunnel
may differ from the device viewing Telegram. Do not add platform collection,
a public short-link service or a Mini App for this release. A copy button with
a 256-character limit cannot carry a typical AWG URI. Files remain outside
collapsed blocks so iOS can download them. Clipboard acceptance must be tested
on an actual iPhone; automated rendering tests cannot establish client behavior.

## 3. AWG preset-aware UDP ports

Generate valid I parameters and select a matching port policy together:

| Preset | Preferred UDP port | Fallback policy |
| --- | --- | --- |
| QUIC | 443 | 8443, 4433, 4443 |
| DNS | 53 | 5353, 5300, 8053; alternate ports have weaker protocol plausibility |
| Chaos | Random 1024–9999 | Up to 128 candidates, wrapping inside that range |

Check availability on the target host through the agent, including UDP
listeners and Docker published bindings. TCP 443 does not occupy UDP 443.
Treat check-then-bind as a race: Docker remains the final binding authority.
Persist the final verified port and show a fallback clearly. A late bind failure
must not be marked successful or trigger replay of an uncertain deployment.

Select automatically on first protocol bootstrap or an explicit preset/port
policy change. Routine Apply settings must not randomize ports, obfuscation
parameters or peer keys. An administrator's explicit port takes precedence and
fails clearly if unavailable. Changing a node's endpoint must invalidate stale
artifacts and reconcile all device configurations.

Tests cover occupied preferred ports, exhausted candidates, Docker conflicts,
ownership checks and verified results. Automatic retry of a late Docker bind
race remains follow-up work: it requires proof that rollback restored the
previous deployment before retrying. Until then, the operation remains blocked
for explicit reconciliation rather than claiming success.

## 4. Exactly one local node

Hide the Local creation option when a local node already exists. Enforce this
rule atomically in the backend as well, including concurrent create requests.
A local node undergoing deletion still occupies that slot until registry
retirement completes. SSH nodes remain independent. Registry-only removal must
warn that removing a record does not uninstall host resources.

## 5. Defaults for future installations

Add administrator-controlled defaults for selected protocols, VLESS transports,
AWG I preset and automatic/manual port policy. Location templates describe
country, region, flag and naming; technical defaults are a separate concern.

Snapshot the defaults into each new node at creation. Changing defaults affects
future nodes only and never silently reconfigures existing installations.
The creation review shows the effective settings and allows overrides.

## 6. Access to future servers

Keep existing bulk actions as a snapshot of currently selected servers. Add
explicit persistent grant policies for all installation servers or selected
regions, including servers added later. A policy stores stable region IDs,
allowed protocols and explicit exclusions; display labels are not identifiers.

Profile freeze/expiry/deletion always overrides a policy. New servers become
eligible according to protocol availability and normal runtime readiness;
reconciliation uses the same transactional outbox as explicit grants.
Moving a server between regions recomputes effective access and requires a clear
administrator review. Deleting a server clears concrete grants while retaining
the policy for future servers. Removing a policy revokes access supplied only
by that policy, without losing independent explicit grants.

The access editor must distinguish “current servers” from “current and future
servers” and explain the effect of regional/all-server grant and revoke actions.

## Delivery order and progress

1. Device schema, legacy identity adoption and backup compatibility.
2. Device-aware durable tasks, configuration issuance and traffic accounting.
3. Authorized device commands/API and Telegram device screens; verify the iOS
   fallback with device configurations.
4. Preset-aware port selection and agent-side availability handling.
5. Single-local-node enforcement and installation defaults.
6. Persistent regional/installation grant policies.

Complete and test each executable slice before enabling it in the interface.
Do not expose multi-device creation while configuration delivery or revocation
still treats all devices as one profile peer. Update progress here with actual
implementation and verification evidence; planned behavior is not delivered
behavior.

### Implemented foundation — 2026-10-07

- [x] Add `backend_devices` with profile ownership, stable UUID/runtime identity,
  display name, lifecycle status and revision; no OS or fingerprint fields.
- [x] Adopt existing AWG grants and historical AWG targets into Device 1 without
  adding remote tasks or changing existing intent payloads. Serialize default
  adoption on the parent profile and do not recreate a retired device.
- [x] Add authorized read-only `GET /api/v1/profiles/{profile_id}/devices`; hide
  internal runtime names and credentials. Include the schema in readiness.
- [x] Include device identities and tombstones in v2 configuration snapshots;
  validate and convert v1 snapshots without changing the original restore
  confirmation checksum.
- [x] Verify the foundation: 895 regression tests passed, 86 environment-dependent
  tests skipped. A separate disposable PostgreSQL run passed 24 device,
  backup and core reliability tests, including concurrent adoption and restore.

### Implemented device-aware execution — 2026-10-07

- [x] Extend the task target to `(operation, node, protocol, device)` and migrate
  existing AWG task metadata without changing command IDs or intent payloads.
  One profile revision produces independent AWG intents for each device;
  profile-based VLESS tasks keep their existing identity.
- [x] Include all devices in grant revocations, profile deletion, freeze, expiry
  and node drain. Skip pending ensures for inactive devices. Node removal waits
  for proof of every peer's revocation, and node summaries consider all active
  peers before reporting a profile grant ready.
- [x] Accept optional AWG `device_id` during configuration issuance. Omission is
  allowed only with exactly one active device; multiple devices require an
  explicit selection. Bind artifacts to profile, node and device revisions and
  recheck device status before and after remote reads. Include the device name
  in AWG labels and filenames; renaming does not rotate the peer's credentials.
- [x] Track independent AWG baselines in `backend_traffic_peers`, then aggregate
  deltas into the existing monthly profile/node/protocol totals. Preserve the
  old peer baseline before new devices write aggregate fields. Failures remain
  unknown, and toggling the global policy excludes traffic during the pause for
  every device. Peer baselines are cleared during restore/reset, not backed up.
- [x] Verify this slice: the full regression run passed 911 tests with 89 skips.
  Subsequent targeted runs passed 13 runtime/readiness tests and 15 configuration
  and device tests. A disposable PostgreSQL run passed 38 device migration,
  traffic, backup, core reliability and removal tests. Actual runtime script
  fixtures also verify independent addresses, cached configurations and scoped
  deletion; simultaneous connections from real clients remain untested.

### Device mutation API — 2026-10-07

- [x] Add authorized, idempotent POST/PATCH/DELETE device commands, with parent
  revision checks for creation and device revision checks for rename/delete.
- [x] Keep custom names unique within a profile; metadata-only renames preserve
  runtime credentials and do not issue remote mutations.
- [x] Retire devices only after all remote revocations are confirmed. Devices
  that have never been provisioned can retire immediately.
- [x] Verify custom name normalization, idempotency, revision conflicts,
  metadata-only rename, ownership checks and confirmed retirement. The full
  regression suite ran 924 tests with 93 environment-dependent skips; the
  separate disposable PostgreSQL run passed 40 tests, including concurrent
  device creation and executor-confirmed peer deletion.

### Telegram device management — 2026-10-07

- [x] Add a Rich Devices screen with custom-name creation, metadata-only rename
  and confirmed destructive deletion. Forms use the existing control message,
  retain command identity on retry and report duplicate names in RU/EN.
- [x] Add paginated AWG device selection; skip the picker when one active
  device remains. Exclude deleting/retired devices, offer creation for an empty
  profile, and return creation started in the picker to the same server.
- [x] Show pending revocation in device cards and a retry screen while the
  server is still provisioning a new device. Navigation invalidates stale
  deletion confirmations. VLESS remains profile-based and the UI explains it.
- [x] Preserve the existing Rich URI and separate iOS link delivery paths;
  configuration artifacts remain bound to the selected device. Real iOS client
  interaction still requires manual testing.
- [x] Verify device UI and existing Telegram flows: 97 tests passed. The full
  regression suite ran 933 tests with 93 environment-dependent skips. A
  subsequent test covers creation returning to the original server picker.

### Preset-aware AWG ports — 2026-10-07

- [x] Generate QUIC/DNS preferred ports and a random bounded Chaos port for new
  installations. Preserve explicit manual ports and stable stored selections.
- [x] Select a free candidate on the target host before changing configuration,
  using IPv4/IPv6 UDP probes and Docker publication inventory. Reuse a running
  owned container's port only after checking its bind mount ownership.
- [x] Return the selected port with the effective configuration digest; store
  it in the controller only after verification. Preserve the original command
  fingerprint and intent so recovery reads the durable result without replay.
- [x] Apply this to bootstrap and settings operations. Switching the AWG preset
  re-enables automatic selection; manually editing the port selects manual mode.
  Show fallback ports in the AWG settings screen in RU/EN.
- [x] Run the full regression suite: 943 tests, 93 environment-dependent skips.
  Subsequent targeted checks cover actual environment/config port propagation,
  Docker dynamic mappings and bootstrap persistence.

### Local node and installation defaults — 2026-10-07

- [x] Enforce exactly one local node inside the serialized backend transaction,
  including concurrent creation and SSH-to-local edits. Disabled/deleting
  records reserve the slot until registry removal. Hide the Local wizard option
  when occupied and handle stale callbacks and a slot taken during creation.
- [x] Add portable installation defaults with authorized reads, revision-checked
  updates and retry-safe absolute replacement. Preserve the defaults in backups;
  apply them to new nodes without rewriting existing inventory.
- [x] Add the Future installations Rich settings screen in RU/EN: protocol and
  transport selection, AWG preset and automatic/manual port policy, and advanced
  portable fields. Changes remain a draft until Save. The creation wizard
  snapshots the choices and displays AWG settings in the review.
- [x] Verify API, Telegram wizard and settings behavior, plus real PostgreSQL
  races for local-node creation and conflicting defaults updates. The separate
  PostgreSQL suite passed 43 device, defaults, backup, reliability, traffic and
  removal tests. The full regression suite ran 958 tests successfully, with
  96 environment-dependent skips.

### Persistent access policy backend — 2026-10-07

- [x] Add stable region catalog IDs and preserve regions after their last node
  is removed. Nodes resolve normalized labels into catalog identities.
- [x] Separate explicit grants from all-server/regional rules and per-protocol
  node exclusions. Materialize their union into the existing grant table without
  adopting inherited grants as manual sources. Removing a rule preserves
  independent manual access and overlapping rules.
- [x] Add authorized read/replace APIs with profile revision checks, exact
  idempotent command identity and the ordinary durable outbox. Bootstrap enrolls
  eligible profiles only after confirmation; expiry/freeze/deletion and all AWG
  device revocations retain their existing execution guards.
- [x] Clear concrete access sources on node drain while retaining future rules.
  Preview region moves, including future bootstrap, and require confirmation
  when they change access. Previously installed nodes retain desired access
  during temporary runtime maintenance.
- [x] Include regions, node-region links and access sources in v3 configuration
  backups. Convert validated v1/v2 snapshots into manual-only sources without
  changing the original confirmation checksum.
- [x] Verify policy overlap/exclusions, future bootstrap, node drain, profile
  freeze/expiry/deletion, HTTP permissions/revisions and region review. Disposable
  PostgreSQL checks cover conflicting edits, simultaneous policy edit/bootstrap,
  and restoring policy and region identities. The full regression suite ran
  975 tests with 99 environment-dependent skips. A final targeted run covers
  restricted node-only credentials and revocation of both AWG devices while
  their earlier ensures are pending.
- [x] Add policy controls to the Telegram access editor, creation/approval
  wizards and region-change confirmation. Preserve current-server bulk actions
  as explicit snapshots and clearly label current-and-future actions.

### Telegram persistent access controls — 2026-10-07

- [x] Add a separate Current and future servers rules screen in RU/EN, with
  protocol selection for the whole installation and stable catalog regions,
  including regions without current nodes. Paginate regions and reject stale
  callbacks from previous pages, messages or completed drafts.
- [x] Show effective protocol selections in the existing server editor. Removing
  inherited access adds a concrete exclusion; restoring it removes that exclusion
  without converting inherited access into a manual grant. Current-server bulk
  revocation preserves rules and excludes current nodes only.
- [x] Save creation and approval wizard rules together with access expiry in one
  backend command. Allow future-only access with no installed servers. Retain
  command identity for retries after an uncertain save.
- [x] Preview region changes before saving and require confirmation when automatic
  access changes. Bind confirmation to the exact draft, revision, user and message;
  invalidate it when the draft changes.
- [x] Verify Rich editor behavior, pagination, exclusions, creation, retry identity
  and region confirmation. The regression suite ran 984 tests with 99
  environment-dependent skips; an additional HTTP test verifies atomic wizard
  save, idempotent replay and rollback of invalid edits. A separate disposable
  PostgreSQL run passed 15 policy, installation defaults, devices and traffic
  tests, including conflicting policy updates and bootstrap races.

Next: manual acceptance checks for future-server rules and device connections.
Late Docker bind race retry remains deferred pending verified rollback support.
Legacy compatibility is optional during alpha development, so further work must
not be delayed by old configuration migration requirements. Independent AWG
connections from real devices passed user acceptance on 2026-10-09; the iOS
copy fallback remains a manual check.
