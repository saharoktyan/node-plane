# Releases

This document defines the release and versioning policy for Node Plane.

## GitHub Actions and cargo-dist

Pushing a version tag runs the generated cargo-dist release workflow. It builds
Workstation for Linux x86_64, Windows x64 and macOS Intel/Apple Silicon, produces
shell and PowerShell installers and SHA-256 checksums, and publishes all artifacts
in one GitHub Release. Prerelease tags remain GitHub prereleases. Pull requests
run release planning only; native Workstation checks run separately.

`dist-workspace.toml` pins cargo-dist and defines the release platforms. Regenerate
`.github/workflows/release.yml` with `dist generate` after changing the configuration;
do not edit the generated workflow by hand. `scripts/build_release_stack.sh` builds
the controller, driver and agent as extra artifacts during the global build.
It also repackages the built Linux Workstation in the old Linux archive format so
existing alpha installations can discover and install the release.

`scripts/tag_release.sh` remains available for local packaging/tag preparation.
GitHub Actions owns release creation. Its `--publish` option can refresh locally
built artifacts on an existing CI-created release; it never creates a competing
release. Wait for the Release workflow before using that option.

## Controller runtime artifact

Each release now includes `node-plane-controller.tar.gz` alongside the driver,
agent and workstation binaries. `scripts/tag_release.sh` builds it automatically
from an explicit allowlist of tracked Python/runtime files and operational
scripts. It excludes Rust sources, documentation, tests, development tools,
virtual environments, local configuration and caches. `CONTROLLER_PACKAGE.json`
records the release tag, version, full commit and SHA-256 of every included file.
`BUILD_COMMIT` is packaged with the same full commit.

The controller must also carry `runtime_assets/`: the driver reads this bundle
when deploying local or SSH node protocols. Packaging and extraction validate
every dependency named by its deployment manifest, including shell scripts,
Python helpers, the AWG Dockerfile and container entrypoint. Through alpha.53,
these assets were accidentally omitted. Existing archive readers from those
releases also reject this newly included directory, so upgrading them requires
running the corrected archive reader/update script from a separately staged
release; using their installed reader alone cannot complete the upgrade.

The controller archive is included in `SHA256SUMS.txt`; publishing verifies its
manifest and its identity against the release tag. Installation and update reject
missing/corrupt archives and never fall back to full-source downloads. Ordinary
archive installations need no checkout; release discovery fetches public release
metadata and remote tag refs only. Unpublished dev HEAD remains a development
checkout workflow using `--from-source`.

## v0.4.3-alpha.54

- Include the complete runtime_assets deployment bundle in the controller archive.
  This fixes protocol bootstrap on archive installations, where the driver could
  not find the runtime manifest or scripts after successful agent installation.
- Accept future manifest-listed directories and file types without an installed
  filename allowlist. Continue rejecting unsafe paths, links, unlisted files and
  checksum mismatches; the build allowlist remains separate.
- Check every deployment manifest dependency during packaging and extraction,
  including AWG container build files. Reject incomplete bundles even when their
  outer package checksums are consistent.
- Explicit node-operation resolution restores runtime helpers before reading or
  retiring an old command. It never replays bootstrap; passive recovery remains
  read-only.
- Validation: 44 archive, recovery guard, journal archival, agent setup and node
  operation tests passed. Live local/SSH protocol bootstrap remains a manual check.
- Clean installations can use this release directly. Existing archive readers
  through alpha.53 cannot read the new runtime directory; upgrading those installs
  needs a separately staged corrected updater or a clean reinstall.

## v0.4.3-alpha.53

- Accept checksummed operational scripts added by later controller releases while
  retaining the explicit build allowlist and archive path, manifest and hash checks.
  Continue verifying the older alpha.51 controller archive layout.
- Inherit the update error trap inside shell functions so failed downloads record
  a terminal result instead of leaving component progress running indefinitely.
- Provide a guarded, explicit recovery helper for the blocked alpha.51 to alpha.52
  archive update. Existing alpha.51 installations need this bridge before a normal
  update; publishing this release does not repair their installed verifier.
- Validation: 29 archive, recovery guard, journal archival and agent setup tests
  passed. Live recovery and VPS acceptance remain manual checks.

## v0.4.3-alpha.52

- Include `scripts/lib/archive_agent_journals.py` in the controller runtime
  archive. Its omission prevented local and SSH agent setup before systemd unit
  creation on archive-based installations.
- Add a packaged-runtime regression test that checks operational library
  completeness and runs journal preparation against a disposable clean host.
- Validation: 23 archive, journal archival and agent setup tests passed. This
  verifies packaging and fixtures; live VPS installation remains a manual check.
- Update the controller, then retry agent setup; a full VPS reinstall is unnecessary.

## v0.4.3-alpha.51

- Fix the SSH password dialog: a clearly labeled masked input remains visible
  above the confirmation buttons in smaller terminals. Empty submissions keep
  the dialog open instead of failing authentication; mouse confirmation preserves
  the typed password.
- Include the verified one-command Linux workstation installer added after
  alpha.50. It discovers published releases, verifies archive checksum and
  executable version, and delegates local PATH installation to `self install`.
- Validation: 70 workstation Rust tests and Clippy passed; six shell installer
  tests cover release selection, corrupt archives, version mismatch and cleanup.
- Linux x86_64 workstation self-update from alpha.50 is available through Settings
  or `node-plane self update`. Restart TUI after updating the local binary.

## v0.4.3-alpha.50

- Named device management with independent AWG peers, device-aware revocations
  and per-device traffic accounting. VLESS remains profile-based. Separate plain
  configuration links preserve the iOS copy workaround.
- Preset-aware automatic AWG UDP port selection, exactly one local node,
  installation defaults and persistent all-server/regional access rules with
  exclusions, atomic access wizards and region-change confirmation.
- Slim controller runtime archives, managed Python 3.12 through uv, and automatic
  release retention of the active and previous installations.
- Explicit workstation self-install/update/uninstall commands, shell PATH setup,
  background update discovery and version/update controls in TUI Settings.
  Linux x86_64 self-updates verify checksums and replace the binary atomically.
- Configuration backups include device identities, stable regions and access
  policies. Legacy validated backups are converted; live old peer migration is
  not required for this alpha development release.
- Real multi-device AWG connections, iOS copy behavior and production installation
  acceptance remain manual checks.
- Preflight: Python discovery ran 985 tests with 99 environment-dependent skips;
  workstation 69, driver 34 and agent 12 Rust tests passed. Workstation Clippy
  and formatting checks passed. Separate disposable PostgreSQL runs cover policy,
  device, defaults and traffic transactions.

## v0.4.3-alpha.49

- First packaged Rust workstation TUI: SSH key enrollment, saved installation
  profiles, install/update workflows, scrollable diagnostics and confirmed recovery.
- Backend and Rich Telegram diagnostics/recovery inventory and workstation audit.
  Audit records selected admin identity, SSH login, workstation/key fingerprints,
  request/operation IDs and verified/unconfirmed key enrollment outcomes, without
  secrets. The TUI requires the matching backend audit module.
- Coordinated update identity/progress verification and atomic environment writes;
  compiled driver release metadata prevents stale agent/runtime version reports.
- Queued announcement retirement, stale rollout/backup maintenance handling and
  pending node/profile teardown improvements; node templates and iOS separate-link UX.
- Schema additions are forward-compatible tables for workstation attribution,
  audit/enrollment and operation support. Configuration restore preserves audit
  history; rollback does not reverse schema changes.
- Linux amd64 driver, agent and workstation archives target Debian 12 (glibc 2.36).
  Host/systemd/SSH acceptance of this release and Windows TUI acceptance remain
  manual checks; local fixtures do not establish production readiness.
- Preflight: Python discovery completed 867 tests (86 environment-dependent
  checks skipped); driver 34, agent 12 and workstation 59 Rust tests passed.
  Workstation Clippy, formatting and shell syntax checks passed.

The goal is to keep the process simple:
- one stable branch
- one integration branch
- predictable version numbers
- clear rules for alpha builds and stable releases

## Branches

### `main`

Stable branch.

Rules:
- only stable releases live here
- versions on `main` use `x.y.z`
- production should track `main`

Examples:
- `0.1.0`
- `0.1.1`
- `0.2.0`

### `dev`

Integration branch.

Rules:
- new work is merged here first
- versions on `dev` use `x.y.z-alpha.N`
- staging or test deployments should track `dev`

Examples:
- `0.2.0-alpha.1`
- `0.2.0-alpha.2`
- `0.2.0-alpha.3`

### `feature/*`

Short-lived working branches.

Rules:
- branch from `dev`
- merge back into `dev`
- do not release directly from `feature/*`

Examples:
- `feature/xray-traffic-fix`
- `feature/admin-ux-cleanup`
- `feature/server-metrics`

## Version Format

Node Plane uses Semantic Versioning with prerelease labels.

Stable releases:

```text
x.y.z
```

Alpha releases:

```text
x.y.z-alpha.N
```

Examples:
- `0.1.0`
- `0.1.1`
- `0.2.0-alpha.1`
- `0.2.0-alpha.2`
- `0.2.0`

The project version is stored in:

- `VERSION`

## How To Increment Versions

### Patch: `x.y.z`

Increase `z` for:
- bug fixes
- small UX fixes
- minor internal improvements
- low-risk maintenance changes

Examples:
- `0.1.0` -> `0.1.1`
- `0.1.1` -> `0.1.2`

### Minor: `x.y.z`

Increase `y` for:
- new features
- new screens
- new admin actions
- meaningful workflow improvements

Examples:
- `0.1.2` -> `0.2.0`
- `0.2.0` -> `0.3.0`

### Major: `x.y.z`

Increase `x` for:
- breaking changes
- incompatible config changes
- incompatible storage or deployment changes
- anything that requires explicit migration or coordination

Examples:
- `0.9.4` -> `1.0.0`
- `1.3.2` -> `2.0.0`

## Alpha Version Rules

Alpha builds are used on `dev`.

Rules:
- start a new development cycle with `x.y.z-alpha.1`
- bump the alpha counter after a meaningful batch of changes
- keep the base version stable until release

Example:

```text
main: 0.1.0
dev:  0.2.0-alpha.1
dev:  0.2.0-alpha.2
dev:  0.2.0-alpha.3
main: 0.2.0
```

Recommended interpretation:
- `alpha.1` — first testable build for the next release
- `alpha.2+` — additional testable builds after fixes or new changes

## Release Tags

Stable releases should always be tagged.

Format:

```text
vX.Y.Z
```

Examples:
- `v0.1.0`
- `v0.1.1`
- `v0.2.0`

Alpha tags are optional.

If used, keep the same format:
- `v0.2.0-alpha.1`
- `v0.2.0-alpha.2`

Recommended minimum:
- tag every stable release
- alpha tags only when they add real value

## Release Workflow

### Normal feature work

1. Create a branch from `dev`.
2. Implement the change in `feature/*`.
3. Merge the change into `dev`.
4. If the result should be tested as a new build, bump `VERSION` on `dev`.
5. Deploy or test from `dev`.

### Starting a new release cycle

After shipping a stable release from `main`:

1. Decide the next target version.
2. Update `VERSION` on `dev` to the next alpha.

Example:
- current stable: `0.1.0`
- next cycle on `dev`: `0.2.0-alpha.1`

### Publishing a stable release

1. Ensure `dev` is in a releasable state.
2. Update `VERSION` from `x.y.z-alpha.N` to `x.y.z`.
3. Merge the release into `main`.
4. Create a git tag `vX.Y.Z`.
5. Deploy production from `main`.

## Tagging Rule

Release tags and `VERSION` must always match.

Examples:
- tag `v0.3.1-alpha.3` requires `VERSION=0.3.1-alpha.3`
- tag `v0.4.0` requires `VERSION=0.4.0`

This repository includes a helper:

- `scripts/tag_release.sh`

The helper:
- validates the tag format
- checks that `VERSION` matches the tag without the `v` prefix
- refuses to tag a dirty tracked worktree
- refuses to overwrite an existing tag

Example:

```bash
./scripts/tag_release.sh v0.3.1-alpha.3
```

Recommended release order:

1. Update `VERSION`.
2. Commit the version bump.
3. Run `./scripts/tag_release.sh vX.Y.Z[-alpha.N]`.
4. Push the branch and tag.

## Recommended Branch Targets

Use this as the default:

- production: `main`
- staging or test bot: `dev`
- daily work: `feature/*`

## Practical Examples

### Example 1: small bugfix after `0.1.0`

If the next release is only a fix release:

- `main`: `0.1.0`
- `dev`: `0.1.1-alpha.1`
- after testing:
  - `main`: `0.1.1`
  - tag: `v0.1.1`

### Example 2: next feature release after `0.1.0`

- `main`: `0.1.0`
- `dev`: `0.2.0-alpha.1`
- more changes:
  - `0.2.0-alpha.2`
  - `0.2.0-alpha.3`
- release:
  - `main`: `0.2.0`
  - tag: `v0.2.0`

## Minimal Policy

If you want the short version, it is this:

- `main` is stable
- `dev` is prerelease
- `feature/*` branches merge into `dev`
- `main` uses `x.y.z`
- `dev` uses `x.y.z-alpha.N`
- stable releases are tagged as `vX.Y.Z`
- `VERSION` is the source of truth for the app version

## Supported stack and compatibility

The supported controller is the systemd installation: PostgreSQL, standalone
backend API, backend worker, aiogram Telegram client and Rust node driver.
Python 3.11 or 3.12 is required. Controller Docker deployment and the removed
PTB client are unsupported. Docker is required on VPN nodes for protocol
containers; Docker Compose is not a controller prerequisite.

| Combination | Support and verification |
| --- | --- |
| Backend, worker, Telegram client and driver from one release commit | Supported controller unit. Keep all Python services on the same `current` release and install the matching driver artifact. |
| Agent and runtime bundle from that same commit | Supported node unit. Use the release's node rollout/bootstrap rather than combining an arbitrary binary with old runtime scripts. |
| Older agent during coordinated rollout | Temporary update/recovery state. Show it as requiring an update or unknown; do not treat successful TCP reachability as feature compatibility. |
| Independently selected backend/client/driver/agent versions | Unsupported unless a future release explicitly documents and tests that combination. Sharing `/api/v1` or protobuf package names is insufficient. |
| PTB release or controller Docker image | Unsupported installation or rollback target. |

Enforcement has specific boundaries. Coordinated updates verify the driver
commit and agent/runtime commits against the requested release. Mismatch or
unavailable verification cannot count as successful installation. Core failure
triggers controller rollback; an agent-only failure leaves a partially successful
update with affected nodes listed. Unknown RPC methods and failed preconditions
are failures, not permission to call legacy provisioning interfaces. Validated
HTTP/RPC inputs and mTLS still apply.

There is no universal startup handshake that rejects every possible mixed
version before any read-only call. Reported versions are observations, not such
a guarantee. Release acceptance therefore tests one coherent commit; operators
must repair a mixed installation before resuming provisioning. Arbitrary mixed
version support and a full compatibility-negotiation protocol are deferred.

## Schema and rollback boundary

Version selection rejects unrecognized versions, major-version downgrades and
minor-version downgrades while the major version is zero. An allowed patch/alpha
downgrade is only a version-policy decision; it does not prove database
compatibility. Schema initialization is forward initialization, with no reverse
migration engine. The base `schema_meta` marker does not describe every backend
service table or certify that old code can read the database.

Every release must classify its database changes as additive and compatible
with the previous supported release, or explicitly breaking. For a breaking
change, record a migration/recovery procedure and require a protected complete
PostgreSQL backup before activation. Do not rely on automatic core rollback to
undo schema or data changes: its snapshot covers controller files, environment,
units and driver, not a reverse database migration. Keep the previous release
directory and matching binaries until acceptance finishes.

Configuration backups contain the documented configuration tables, not bearer
credentials, operational journals, retirement evidence, agent journals or VPN
runtime files. Restoration verifies checksum and table columns, blocks other
mutations, revokes current access and restores nodes disabled/unapplied. It is
not a substitute for a PostgreSQL plus secret/runtime disaster-recovery backup.
An incompatible snapshot must be rejected instead of force-imported.

For failed rollback or manual recovery:

1. Inspect the update job, durable progress file and systemd journals. Quiesce
   the update process/unit and worker before changing the release or driver;
   stopping the worker timer alone does not stop an already launched updater.
2. Preserve the database, shared environment, adapter credentials, installation
   UUID, mTLS material and local/remote journals. Choose a previously accepted
   complete stack whose database compatibility has been established. If it is
   incompatible, use a separately planned database recovery in the maintenance
   window, followed by reconciliation with node journals and actual host state.
3. Restore the matching controller files/driver/units, verify backend readiness
   and binary identities, then restart the Telegram client. `rollback.sh` only
   changes the Python release and restarts services; it is not complete stack
   or schema recovery and must not be presented as such.
4. Use the update result's **Recheck result** action only after the old update
   process is quiescent and the repaired stack is proven. An uncertain result
   keeps the gate closed. Missing launch identity or an unconfirmed rollback
   still requires operator investigation; deleting gate rows or command journals
   is not a supported recovery procedure.
5. Resume the worker after these checks. Recover uncertain profile/settings
   commands through the existing audited repair contract; never send them again
   with new command IDs merely to clear an error.

## Certificate renewal and trust recovery

Controller mTLS material lives in `shared/driver-agent-tls`. The CA private key
and driver client private key stay on the controller. Agents receive their own
server certificate/key and the public CA, with DNS/IP SAN matching the target.
Protect private directories and keys; keep an independent working SSH access
path for recovery. Never disable TLS or host verification to work around expiry.

The current rollout issues 825-day leaves and renews them when fewer than 30
days remain, the key does not match, CA verification fails or the node SAN
changes. This happens during setup/rollout, not through an autonomous renewal
timer. Check expiry before scheduling rollout; an unreachable host cannot have
its installed certificate renewed. Incomplete controller CA/client material
is rejected rather than silently replaced. A CA with fewer than 180 days left
also prevents rollout until deliberate CA rotation.

For routine leaf renewal, use node Agent setup/rollout with the existing
installation identity, then verify driver-to-agent mTLS and the installed
certificate. Trusted local rollout may use `setup_driver_agents.sh` with
`--backend-node-key` and the existing local/SSH connection arguments; do not
enroll a different node identity just to renew a certificate.

CA rotation is a planned outage or a trust-recovery operation, not automatic:

1. Inventory every enrolled node and prove independent SSH/local access. Stop
   provisioning and quiesce workers, updater and driver. Resolve legacy agents'
   controller ownership before changing their CA; agents without an installation
   marker use the old CA as conservative ownership evidence.
2. Retain the controller installation UUID and agent journals. Securely archive
   the old TLS directory with its fingerprint and date, outside the active
   `driver-agent-tls` path. Do not copy old leaves into the replacement directory.
3. Let the existing setup workflow create a new complete CA/client pair and
   reissue server leaves for each registered node using its real target/SAN.
   Install the new public CA on every agent through the verified management
   connection. The CA private key must remain on the controller. Stop or isolate
   nodes that cannot be updated; track them explicitly as incomplete.
4. Verify certificate chains, SANs and actual mTLS connectivity for each node,
   then resume only the coherent new trust domain. Keep protected old material
   for planned-rotation recovery; never restore compromised trust to service.

There is no per-certificate CRL/OCSP revocation implementation. Removing a node
from the registry is not certificate revocation. For a compromised node server
key, isolate the old host/endpoint and reissue the affected leaf through verified
management access. A compromised CA or shared driver client key requires a new
trust domain across all remaining agents: reissuing a leaf alone cannot invalidate
the stolen certificate. Automatic rotation and fine-grained revocation remain
future capabilities. Live rotation acceptance requires a disposable host.

## Systemd release checklist

Use the repository release scripts; a release request does not require building
or publishing controller Docker images. The Debian builder container is a build
tool, independent of the supported installation mode.

1. Commit the reviewed changes, update `VERSION` and select its matching tag.
   Record schema compatibility, rollout/recovery changes and pending manual
   acceptance in the release notes. Include the unissued cleanup, backup,
   registry-removal and UI changes when this batch is released.
2. Run Telegram compilation, the Python suite and both Rust suites. Run the
   opt-in PostgreSQL regressions separately against an isolated database;
   normal discovery skips environment-dependent checks. Distinguish fixture
   evidence from actual SSH, systemd scheduling and Telegram client acceptance.
3. Build through `scripts/build_release_in_container.sh vX.Y.Z[-alpha.N]` for
   Linux amd64 on Debian 12. This delegates packaging to `tag_release.sh`.
   The archives must contain executable driver/agent ELF binaries requiring
   glibc no newer than 2.36. Other architectures are not currently published.
4. Verify all controller and binary `*.tar.gz` assets against `SHA256SUMS.txt` and the release metadata
   against the tag, `VERSION` and commit. Publishing rechecks checksums and ELF
   compatibility; checksums detect corruption, they are not artifact signatures.
5. On a disposable clean host, verify installation creates backend, worker
   service/timer, driver and Telegram units, retires any old polling unit, and
   passes `/health/ready` and the installation healthcheck. Agent installation,
   protocol bootstrap, member approval and AWG/VLESS issuance must complete.
6. On an upgraded host, verify shared configuration, installation identity,
   PostgreSQL records, node targets and journals survive. Exercise coordinated
   core rollback and agent-only partial failure; confirm progress can be reopened
   after navigation/restart. Preserve the previous accepted release for recovery.
7. Use the tag/publish script flow with existing verified binaries (`--no-build`)
   and inspect the uploaded assets. Label alpha releases as prereleases. Record
   any unperformed clean/upgrade/SSH/client checks rather than implying they ran.
