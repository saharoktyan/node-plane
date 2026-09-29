# Install

This guide covers installation and operational basics for both supported Node Plane deployment modes.

## Before You Start

- decide whether this host will run the bot directly (`Simple Mode`) or only act as the bot control plane for remote nodes (`Portable Mode`)
- make sure you have a valid Telegram bot token and know the numeric Telegram user id that should become the first admin
- if you plan to manage remote nodes, verify SSH access before touching Node Plane
- if you use `Portable Mode`, decide which published GHCR tag you want to run
- keep the source checkout separate from the runtime install root in `Simple Mode`

## Requirements

- Python `3.11` or `3.12`
- `Simple Mode` automatically picks `python3.12` or `python3.11` even when the system `python3` is newer. Install the matching `python3.12-venv` or `python3.11-venv` package as well; set `NODE_PLANE_PYTHON_BIN=/path/to/python3.12` to choose a specific interpreter.
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

### Portable Mode

Use this when the bot should run separately and manage nodes remotely.

- bot runs in Docker via `docker compose`
- all managed nodes are connected over `ssh`
- runtime images are pulled from `ghcr.io/saharoktyan/node-plane`
- best fit for multi-node setups

Important constraint:
`local` node deployment is supported only in `Simple Mode`. If the bot runs in Docker, managed nodes must be added via `ssh`.

## Simple Mode

### Recommended path: install with `install.sh`

```bash
git clone https://github.com/saharoktyan/node-plane.git node-plane-src
cd node-plane-src
./scripts/install.sh --mode simple
```

If you prefer SSH cloning, add a GitHub SSH key to the host first and then use:

```bash
git clone git@github.com:saharoktyan/node-plane.git node-plane-src
```

To start the full stack without an interactive systemd prompt:

```bash
./scripts/install.sh --mode simple --install-systemd
```

The script will prompt for missing values and prepare the release layout for you.
It also asks which git tag/ref to install, with the default set to the latest release tag for the selected branch.

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

- source checkout: `/opt/node-plane-src`
- install root: `/opt/node-plane`

Do not place the git checkout inside the install root. `Simple Mode` exports releases under `NODE_PLANE_BASE_DIR/releases` and maintains the active app under `NODE_PLANE_BASE_DIR/current`.

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
4. Apply node settings to deploy the selected VPN protocols
5. Create a profile, grant access, and issue a config

You can later add more remote nodes over `ssh` from the same bot.

### Testing from the development branch

The stable `v0.4.2` release predates the full aiogram installation flow. To test
the current stack before the next release, select the development branch and
its head explicitly:

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
- `NODE_PLANE_SOURCE_DIR`: source checkout path
- `NODE_PLANE_INSTALL_MODE`: `simple` or `portable`
- `NODE_PLANE_INSTALL_REF`: records the tag/ref selected at the last installation; the next run fetches tags and defaults to the latest release tag for `NODE_PLANE_UPDATE_BRANCH`. Use `--ref <tag>` (or an exported `NODE_PLANE_INSTALL_REF`) to pin a specific version.
- `DB_BACKEND`: should be `postgres` for `0.4`
- `POSTGRES_DSN`: PostgreSQL DSN used for runtime storage; optional if you let the installer/update path auto-provision PostgreSQL
- `SSH_KEY`: SSH private key used for remote node management
- `NODE_PLANE_IMAGE_REPO`: GHCR image repo for `Portable Mode`
- `NODE_PLANE_IMAGE_TAG`: image tag for `Portable Mode`
- `UPDATE_CHECK_INTERVAL_SECONDS`: periodic update check interval
- `UPDATE_CHECK_FIRST_DELAY_SECONDS`: initial delay before the first update check

See [.env.example](.env.example) for the full template.

## Operations

Inspect the current setup:

```bash
./scripts/healthcheck.sh
./scripts/healthcheck.sh --mode simple
./scripts/healthcheck.sh --mode portable
```

Update an existing deployment:

```bash
./scripts/update.sh
./scripts/update.sh --mode simple
./scripts/update.sh --mode portable
```

Driver/agent rollout (the only driver mode is gRPC):

```bash
./scripts/setup_driver_agents.sh
./scripts/setup_driver_agents.sh --dry-run
```

- `update.sh --mode simple` now runs `setup_driver_agents.sh` automatically by default (`NODE_PLANE_AUTO_SETUP_DRIVER_AGENTS=1`).
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
./scripts/cleanup_releases.sh --dry-run
./scripts/cleanup_releases.sh
./scripts/check_updates.sh
```

## Common Pitfalls

- do not place the git checkout inside `NODE_PLANE_BASE_DIR` in `Simple Mode`; the installer expects a separate source checkout and release root
- do not try to register the current Docker host as a `local` node in `Portable Mode`; use `ssh`
- do not leave `BOT_TOKEN=replace_me` or `ADMIN_IDS=123456789` in `.env`
- if `POSTGRES_DSN` is empty, make sure the host allows `install.sh` or `update.sh` to install Docker and start the runtime PostgreSQL container
- make sure the SSH key in `SSH_KEY` is readable by the process that runs the bot
- if `Portable Mode` uses GHCR images, confirm that `NODE_PLANE_IMAGE_TAG` actually exists before running updates
- if first bootstrap fails, rerun `Probe` and fix the reported host issues before retrying `Bootstrap`

## Development

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt
python3 -m unittest discover -s tests
```
