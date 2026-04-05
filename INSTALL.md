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
- runtime images are pulled from `ghcr.io/seventh7dev/node-plane`
- best fit for multi-node setups

Important constraint:
`local` node deployment is supported only in `Simple Mode`. If the bot runs in Docker, managed nodes must be added via `ssh`.

## Simple Mode

### Recommended path: install with `install.sh`

```bash
git clone https://github.com/seventh7dev/node-plane.git node-plane-src
cd node-plane-src
./scripts/install.sh --mode simple
```

If you prefer SSH cloning, add a GitHub SSH key to the host first and then use:

```bash
git clone git@github.com:seventh7dev/node-plane.git node-plane-src
```

If you want the installer to place and start the systemd unit automatically:

```bash
./scripts/install.sh --mode simple --install-systemd
```

The script will prompt for missing values and prepare the release layout for you.

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
- a generated systemd unit under `scripts/node-plane.service`

### First run after installer setup

After install:

```bash
./scripts/healthcheck.sh --mode simple
```

Then in Telegram:

1. Open the bot from the account listed in `ADMIN_IDS`
2. Send `/start`
3. Choose `Set up this server`
4. Open the created server card
5. Run `Probe`
6. Run `Bootstrap`

You can later add more remote nodes over `ssh` from the same bot.

### Manual setup

Use this only if you want to inspect or reproduce what the installer does.

1. Prepare `.env` with `BOT_TOKEN`, `ADMIN_IDS`, and the `NODE_PLANE_*` paths. `POSTGRES_DSN` is optional if you want the script to auto-provision PostgreSQL.
2. Create the release layout under the install root.
3. Create a Python virtualenv inside the active release.
4. Install dependencies from `requirements.txt`.
5. Run `app/manage_db.py init` with the correct `NODE_PLANE_*` environment.
6. Create a `systemd` unit that points to the active release under `/opt/node-plane/current`.
7. Start the service, then run `./scripts/healthcheck.sh --mode simple`.

In practice, the script already performs these steps and is the preferred path.

## Portable Mode

### Recommended path: install with `install.sh`

```bash
git clone https://github.com/seventh7dev/node-plane.git node-plane-src
cd node-plane-src
./scripts/install.sh --mode portable
```

If you prefer SSH cloning, add a GitHub SSH key to the host first and then use:

```bash
git clone git@github.com:seventh7dev/node-plane.git node-plane-src
```

The script will prompt for missing values and write the portable-mode settings into `.env`.

For a predictable non-interactive setup, configure `.env` first with at least:

```env
BOT_TOKEN=...
ADMIN_IDS=123456789
SSH_KEY=/root/.ssh/id_ed25519
NODE_PLANE_IMAGE_REPO=ghcr.io/seventh7dev/node-plane
NODE_PLANE_IMAGE_TAG=<release-tag>
DB_BACKEND=postgres
```

`Portable Mode` can also leave `POSTGRES_DSN` empty. The installer will populate it automatically for the bundled `postgres` compose service.

### Start the bot

```bash
docker compose pull
docker compose up -d
```

Then validate:

```bash
./scripts/healthcheck.sh --mode portable
```

### Prepare the first remote node

In Telegram:

1. Open the bot from the account listed in `ADMIN_IDS`
2. Send `/start`
3. Choose `Set up over SSH`
4. Open `Admin -> SSH Key`
5. Copy the generated public key
6. Add it to `~/.ssh/authorized_keys` on the target server
7. Open the created server card
8. Run `Probe`
9. Run `Bootstrap`

### Manual setup

Use this only if you want to reproduce the installer manually.

1. Prepare `.env` with `BOT_TOKEN`, `ADMIN_IDS`, `SSH_KEY`, `NODE_PLANE_IMAGE_REPO`, and `NODE_PLANE_IMAGE_TAG`.
2. Pull the selected image from GHCR with `docker compose pull`.
3. Start the bot with `docker compose up -d`.
4. Validate the containerized setup with `./scripts/healthcheck.sh --mode portable`.
5. Complete SSH key onboarding and node bootstrap from Telegram.

For normal use, the installer-first path above is the intended one.

## Environment Variables

Key variables:

- `BOT_TOKEN`: Telegram bot token
- `ADMIN_IDS`: comma-separated Telegram numeric user IDs with admin access
- `NODE_PLANE_BASE_DIR`: install root, usually `/opt/node-plane`
- `NODE_PLANE_APP_DIR`: active app path, usually `/opt/node-plane/current`
- `NODE_PLANE_SHARED_DIR`: shared state path, usually `/opt/node-plane/shared`
- `NODE_PLANE_SOURCE_DIR`: source checkout path
- `NODE_PLANE_INSTALL_MODE`: `simple` or `portable`
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
