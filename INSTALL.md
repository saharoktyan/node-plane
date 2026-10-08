# Install

This guide covers the supported systemd stack: backend, worker, aiogram client,
Rust driver and managed node agents. The old PTB bot is removed.

## Before You Start

- use `Simple Mode` on the controller host for both local and remote nodes
- make sure you have a valid Telegram bot token and know the numeric Telegram user id that should become the first admin
- if you plan to manage remote nodes, verify SSH access before touching Node Plane
- keep the temporary installer directory separate from the runtime install root

## Requirements

- Python `3.11` or `3.12`
- `Simple Mode` automatically picks `python3.12` or `python3.11` even when the system `python3` is newer. set `NODE_PLANE_PYTHON_BIN=/path/to/python3.12` to choose a specific interpreter.
- Telegram bot token
- your Telegram numeric user id in `ADMIN_IDS`
- Docker will be installed automatically on supported Linux hosts when Node Plane needs it for runtime PostgreSQL
- SSH access to target nodes if you manage remote hosts

## Choose A Mode

### Simple Mode

Use this when you want the shortest path to a working deployment.

- bot runs directly on the host
- intended for `systemd` + Python venv setup
- supports same-host runtime deployment
- can also manage additional remote nodes over `ssh`
- best fit for a single VPS

Controller Docker/Compose installation is unsupported. Docker is still used
for managed VPN protocols and optional local PostgreSQL provisioning.

## Simple Mode

### Recommended path: install with `install.sh`

An initial native [workstation CLI/TUI](rust/node-plane-cli/README.md) can now
drive this systemd installation over embedded SSH with masked password/token
input and seven-step progress. It also supports coordinated backend-owned updates,
basic diagnostics and controller public-key enrollment on target nodes.
It is under development; complete VPS and manual
Windows acceptance remain pending. The direct script workflow below remains
supported.

The controller is distributed as `node-plane-controller.tar.gz`. Download the
small stdlib-only bootstrap helper, then let it select and verify the release:

```bash
curl -fsSL https://raw.githubusercontent.com/saharoktyan/node-plane/dev/scripts/controller_release.py -o /tmp/node-plane-controller-release.py
python3 /tmp/node-plane-controller-release.py download --branch dev --destination /opt/node-plane-install
cd /opt/node-plane-install
./scripts/install.sh --mode simple
```

Use `--branch main` for stable releases, or add `--ref vX.Y.Z[-alpha.N]` for an
exact published release. Python, Git (remote ref metadata only) and CA
certificates must be installed. No repository clone is needed. The helper checks
the archive checksum, tag, full commit and file manifest before extraction.
Only releases containing the controller archive and checksum asset are admitted.

A developer checkout remains supported: use `scripts/install.sh --from-source`
to export its selected Git ref. This explicit mode can include development files
and is not the normal distribution path. Older releases without the controller
asset require this mode; missing or invalid archives never trigger a silent
full-source download.

To start the full stack without an interactive systemd prompt:

```bash
./scripts/install.sh --mode simple --install-systemd
```

The script will prompt for missing values and prepare the release layout for you.
It selects a published controller archive in the chosen update channel. A downloaded
controller package can be reused without another download.

For a predictable non-interactive setup, configure `.env` first with at least:

```env
BOT_TOKEN=...
ADMIN_IDS=123456789
NODE_PLANE_BASE_DIR=/opt/node-plane
NODE_PLANE_APP_DIR=/opt/node-plane/current
NODE_PLANE_SHARED_DIR=/opt/node-plane/shared
DB_BACKEND=postgres
```

`POSTGRES_DSN` is optional for the installer path. If it is empty, `install.sh` will try to install Docker and provision a local PostgreSQL container automatically.

Recommended layout:

- temporary installer directory: `/opt/node-plane-install`
- install root: `/opt/node-plane`

Keep the installer directory outside the install root. `Simple Mode` stores verified
controller releases under `NODE_PLANE_BASE_DIR/releases` and maintains the active
app under `NODE_PLANE_BASE_DIR/current`. The archive contains Python runtime
modules, requirement files, the license and operational scripts; Rust sources,
workstation sources, tests, documentation and build tools are excluded.
Updates run from the active controller and use release archives, preserving the
existing health verification and rollback flow. Source checkouts are unnecessary
after installation. Unpublished dev HEAD updates require `--from-source` and a
development checkout; archive installations offer published releases only.

### What the installer prepares

`Simple Mode` creates:

- release history under `/opt/node-plane/releases/`
- active release symlink under `/opt/node-plane/current`
- shared runtime state under `/opt/node-plane/shared/`
- initialized backend schema and administrator accounts from `ADMIN_IDS`
- a private Telegram adapter credential under the shared data directory
- `node-plane-backend.service`, `node-plane-backend-worker.timer`, and `node-plane-telegram.service`

The installer disables the old `node-plane.service` when it starts the aiogram
client. Only the new client polls the Telegram token.

### First run after installer setup

After install:

```bash
./scripts/healthcheck.sh --mode simple
```

Then in Telegram:

1. Open the bot from the account listed in `ADMIN_IDS`
2. Send `/start`
3. Add a local or SSH node, then install its agent
4. Open Bootstrap, install Docker if needed, and deploy the selected VPN protocols
5. Create a profile, grant access, and issue a config

You can later add more remote nodes over `ssh` from the same bot.

### Testing from the development branch

Use a current aiogram/backend release. PTB-only releases are unsupported by
the current installer and rollback workflow. To test development releases:

```bash
./scripts/install.sh --mode simple --branch dev
```

The installer prepares the backend, worker, Telegram adapter credential, and
aiogram service. Docker Compose installation is unsupported while the project
finishes the systemd migration.

## Environment Variables

Key variables:

- `BOT_TOKEN`: Telegram bot token
- `ADMIN_IDS`: comma-separated Telegram numeric user IDs with admin access
- `NODE_PLANE_BASE_DIR`: install root, usually `/opt/node-plane`
- `NODE_PLANE_APP_DIR`: active app path, usually `/opt/node-plane/current`
- `NODE_PLANE_SHARED_DIR`: shared state path, usually `/opt/node-plane/shared`
- `NODE_PLANE_SOURCE_DIR`: active controller path for archive installations; source checkout in development mode
- `NODE_PLANE_INSTALL_MODE`: `simple`
- `NODE_PLANE_INSTALL_REF`: records the tag/ref selected at the last installation; the next run fetches tags and defaults to the latest release tag for `NODE_PLANE_UPDATE_BRANCH`. Use `--ref <tag>` (or an exported `NODE_PLANE_INSTALL_REF`) to pin a specific version.
- `DB_BACKEND`: should be `postgres` for `0.4`
- `POSTGRES_DSN`: PostgreSQL DSN used for runtime storage; optional if you let the installer/update path auto-provision PostgreSQL
- `SSH_KEY`: SSH private key used for remote node management

See [.env.example](.env.example) for the full template.

## Operations

Inspect the current setup:

```bash
./scripts/healthcheck.sh
./scripts/healthcheck.sh --mode simple
```

Update an existing deployment:

```bash
./scripts/update.sh
./scripts/update.sh --mode simple
```

Driver/agent rollout (the only driver mode is gRPC):

```bash
./scripts/setup_driver_agents.sh
./scripts/setup_driver_agents.sh --dry-run
```

- The backend coordinates driver and agent rollout during stack updates. The standalone controller updater does not independently schedule a second agent rollout.
- A node registered with `transport=local` gets a node-agent systemd service on
  the controller itself. It listens only on `127.0.0.1` with mutual TLS; the
  driver target is recorded as `<node-key>=127.0.0.1:50061`. Use **Set up agent**
  in the node's Bootstrap menu if this is an existing installation. Only one
  local node can use the controller's agent service.
- To disable automatic rollout during update, set `NODE_PLANE_AUTO_SETUP_DRIVER_AGENTS=0`.
- Simple-mode post-install rollout is enabled by default; disable it with `NODE_PLANE_AUTO_SETUP_DRIVER_AGENTS_ON_INSTALL=0`.
- Binary source policy for driver/agent rollout:
  - `NODE_PLANE_BIN_SOURCE=auto` (default): use GitHub release binaries first, then build with Cargo if available. If neither is available, the script asks before installing Rust build tools. In the bot, confirm with **Install Cargo and continue**; for unattended runs, set `NODE_PLANE_INSTALL_RUST=yes` to allow installation or `no` to stop. Before local compilation, the script requires at least 2048 MiB available RAM and CPU busy time at or below 65%; Cargo defaults to one build job.
  - `NODE_PLANE_BIN_SOURCE=release`: only download release binaries (Rust toolchain not required).
    If GitHub's direct asset URL returns 404 for a published public release,
    the installer retries that asset through the GitHub Releases API.
  - `NODE_PLANE_BIN_SOURCE=build`: only local `cargo build --release`.
- Local build limits can be adjusted with `NODE_PLANE_BUILD_MIN_MEM_MB` and
  `NODE_PLANE_BUILD_MAX_CPU_PERCENT`. If a VPS does not meet them, build and
  publish the release artifacts on another machine, then use release mode.
- Published Linux amd64 binaries must work with glibc 2.36 (Debian 12) or older.
  A native build on a newer distribution may not start on older nodes. On a
  machine with Docker or Podman, prepare portable artifacts with
  `./scripts/build_release_in_container.sh <tag>` (use `sudo` if Docker requires
  it; build outputs remain owned by the checkout owner), then create/push the tag and
  publish those artifacts with `./scripts/tag_release.sh <tag> --no-tag --no-build --publish --no-draft`.
  The release script rejects binaries that require a newer glibc. Agent rollout
  also checks the staged binary on each node before replacing the installed one.
- The rollout generates a private driver-agent CA and mutual TLS identities in
  `${NODE_PLANE_SHARED_DIR}/driver-agent-tls/`. Keep this directory private and
  backed up with the controller; the CA private key is never installed on nodes.
  It uses `openssl` locally and requires sudo access over SSH to install
  certificates on each managed node. Agent calls fail closed when TLS settings
  or valid certificates are missing.
- Release asset source can be tuned with:
  - `NODE_PLANE_GITHUB_REPO`
  - `NODE_PLANE_BINARY_RELEASE`
  - `NODE_PLANE_DRIVER_ASSET_NAME`
  - `NODE_PLANE_AGENT_ASSET_NAME`
  - `NODE_PLANE_DRIVER_BIN_URL`
  - `NODE_PLANE_AGENT_BIN_URL`
- Safe preflight without host changes:
  - `./scripts/setup_driver_agents.sh --dry-run`
  - validates release URL reachability or local build availability, plus SSH connectivity to target nodes

Maintenance:

```bash
./scripts/rollback.sh --to <release-id>
./scripts/check_updates.sh
```

Release retention is automatic after successful activation and health checks.
Only the active release and the actual previous working release remain. The
`previous` symlink records the rollback target; ordering by directory timestamps
does not determine protection. Failed updates and `--skip-restart` preparation
never prune releases. Manual release cleanup has been removed from the bot and
API. Backups and node command journals have separate retention policies.

The installer downloads a pinned standalone uv binary, compatible with Debian
12 and Ubuntu 22.04 or newer. Existing Python 3.11/3.12 is preferred; when neither
is available (for example, Ubuntu 22.04 or 26.04), uv provisions a private Python 3.12
under `${NODE_PLANE_SHARED_DIR}/tools/python`, leaving system Python unchanged.
Explicit `NODE_PLANE_PYTHON_BIN` choices are validated rather than replaced.
The installer also provisions missing download, SSH, certificate, systemd and
basic host utilities; Docker/PostgreSQL provisioning remains automatic.
uv creates environments and installs dependencies without pip/setuptools.
uv lives under `${NODE_PLANE_SHARED_DIR}/tools/`; its shared package cache is
`${NODE_PLANE_SHARED_DIR}/cache/uv`. Separate release environments use hardlinks
from this cache (with copying when the filesystem cannot link), so identical
dependencies do not require another physical copy for each release. Do not
modify installed package files in place. Cache pruning follows successful
release retention. `NODE_PLANE_UV_BIN` can select a pre-provisioned uv executable
for offline deployments and tests.

## Common Pitfalls

- keep the temporary installer directory (or development checkout) outside `NODE_PLANE_BASE_DIR`
- do not leave `BOT_TOKEN=replace_me` or `ADMIN_IDS=123456789` in `.env`
- if `POSTGRES_DSN` is empty, make sure the host allows `install.sh` or `update.sh` to install Docker and start the runtime PostgreSQL container
- make sure the SSH key in `SSH_KEY` is readable by the process that runs the bot
- if first bootstrap fails, rerun `Probe` and fix the reported host issues before retrying `Bootstrap`

## One-time recovery: alpha.51 rejects the alpha.52 archive

The alpha.51 verifier rejects the newly packaged agent journal helper before
applying the update. Its shell error trap can leave the presentation receipt
at `running`. Do not edit database gates or mark the operation successful.

`scripts/recover_archive_update.py` is a narrow bridge for this exact failure.
It requires the blocked stack job, its pinned commit, no dispatched agent
updates, an inactive original unit, the original alpha.51 installation and
journal evidence of one archive rejection before any installation step.
It extends the old verifier in memory for this one known library only and
retains checksum/path/commit validation. The installed package is not modified.
With `--apply`, it stages the verified alpha.52 package and explicitly launches
its updater under the original unit/job identity. Staging is retained; a second
launch is refused if staging already exists, preserving uncertain outcomes.

On the controller as root, download the helper, then validate first:

```sh
curl -fsSL https://raw.githubusercontent.com/saharoktyan/node-plane/dev/scripts/recover_archive_update.py -o /tmp/node-plane-recover-archive.py
NODE_PLANE_APP_DIR=/opt/node-plane/current \
NODE_PLANE_SHARED_DIR=/opt/node-plane/shared \
PYTHONPATH=/opt/node-plane/current/app \
/opt/node-plane/current/.venv/bin/python /tmp/node-plane-recover-archive.py JOB_UUID
```

Replace `JOB_UUID` with the backend update ID, not the local workstation
operation ID. After validation, repeat the last command with `--apply`, observe
the printed update unit journal and resume the original workstation operation
after the update service finishes. The backend rechecks durable completion;
this helper does not clear its gate or change the job record.

## Development

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt
python3 -m unittest discover -s tests
```
