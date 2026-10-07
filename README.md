# Node Plane

Node Plane is a Telegram-based control plane for self-hosted secure connectivity.

It is built for operators who want one interface for node setup, runtime bootstrap, profile management, access delivery, and day-to-day operations across infrastructure they control themselves.

Instead of juggling shell scripts, scattered configs, and ad-hoc server notes, you manage the full lifecycle from a Telegram admin flow: register a node, validate it with `Probe`, deploy runtime with `Bootstrap`, create profiles, and deliver connection configs to users.

The supported deployment is the bundled `install.sh` systemd workflow: a
standalone backend, worker, aiogram Telegram client and Rust driver.

Development priorities and remaining work: [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md).
Architecture: [CORE_ARCHITECTURE.md](CORE_ARCHITECTURE.md).
Protocol configuration: [PROTOCOL_REFERENCE.md](PROTOCOL_REFERENCE.md).

## Why Node Plane

- one control surface for server and user operations
- built for self-hosted infrastructure, not a hosted service
- supports both single-server and multi-node setups
- combines provisioning, diagnostics, access delivery, and updates
- keeps operator workflow inside Telegram instead of a pile of manual steps

## What It Does

- manages nodes from a Telegram admin interface
- provisions targets over `local` host access or `ssh`
- supports `Xray Reality (VLESS)` and `AmneziaWG`
- creates and maintains user profiles with access control
- delivers connection material to end users through the bot
- stores control-plane state in PostgreSQL
- includes diagnostics, telemetry, updates, rollback, and automatic release retention
- supports Russian and English UI

## Deployment Modes

### Workstation quick start (Linux x86_64)

Run this on your own computer to install the workstation TUI, then use it to
install/manage a VPS over SSH:

```sh
curl -fsSL https://raw.githubusercontent.com/saharoktyan/node-plane/dev/scripts/install_workstation.sh | bash
```

The installer downloads the latest published alpha/stable workstation, verifies
SHA256 and its version, and runs `self install` without sudo. Open a new terminal
after installation and run `node-plane`. No Python or external SSH client is
needed on the workstation; the download script requires Bash, curl, tar and
sha256sum. For a pinned release, add `bash -s -- --tag v0.4.3-alpha.50` after the
pipe. The script installs the local assistant; it does not install a controller
on your computer. Details: [workstation guide](rust/node-plane-cli/README.md).

### Simple Mode

Use this when you want the shortest path to a working deployment.

- backend, worker and aiogram client run directly on the host
- intended for `systemd` + Python venv setup
- supports same-host runtime deployment
- can also manage additional remote nodes over `ssh`
- best fit for a single VPS

Controller Docker/Compose installation is unsupported. The retired PTB bot
and its Docker entrypoint have been removed. Managed VPN protocols and the
optional installer-managed PostgreSQL runtime still use Docker; the container
used to build compatible release binaries is also retained.

## Supported Runtime

- `VLESS` over `Xray Reality`
  - transports: `tcp`, `xhttp`
- `AmneziaWG`

Current upstream images used by the project:

- Xray: `ghcr.io/xtls/xray-core:26.3.27`
- AWG: `amneziavpn/amneziawg-go:3.1.20260828`

For AWG nodes, Node Plane builds and deploys its own wrapper image during bootstrap.
AWG 3.1 requires a compatible client (AmneziaVPN 5.0.1.5 or newer, or a native
AWG 3.1 client). After an existing node is migrated, users must import a newly
issued `.conf` or `vpn://` key; previously downloaded configs can stop working.

## Main Workflow

1. Deploy the systemd stack in `Simple Mode`.
2. Open the bot from the Telegram admin account.
3. Send `/start` and create the first managed server.
4. Install the local or SSH node agent, then use `Probe` to check readiness.
5. Use `Bootstrap` to install Docker when needed and deploy protocols.
6. Create one or more profiles.
7. Let users request or receive connection configs through the bot.
8. Use sync, diagnostics, telemetry, update, and rollback flows for ongoing operations.

## Quick Start

### Simple Mode

```bash
git clone https://github.com/saharoktyan/node-plane.git node-plane-src
cd node-plane-src
./scripts/install.sh --mode simple
```

Then follow the full guide in [INSTALL.md](INSTALL.md).

Portable Mode is temporarily unsupported. The simple-mode installer starts the
backend, worker timer, and aiogram Telegram client as systemd services.

If you prefer SSH for cloning, configure a GitHub SSH key on the host first and then use:

```bash
git clone git@github.com:saharoktyan/node-plane.git node-plane-src
```

## Features

### Node Operations

- register nodes as `local` or `ssh`
- enable protocols per node
- validate node readiness with `Probe`
- bootstrap and reinstall runtime
- open ports, install Docker, and sync runtime settings from the bot

### Profile And Access Management

- create named profiles
- assign one or more access methods to a profile
- control access approval
- keep user and profile state in PostgreSQL

### User Delivery

- issue connection material through Telegram
- provide `Xray` links and QR output
- provide `AWG` direct links, QR, `.vpn`, and `.conf` files

### Operations And Maintenance

- node health checks and diagnostics through the Rust driver
- administrator-controlled monthly traffic usage reporting and configurable alerts
- scripted updates with rollback support
- automatic Docker and PostgreSQL runtime provisioning during install/update
- automatic release retention

## Operator Experience

Node Plane is opinionated about the actual workflow operators go through:

- first bring a node into a known-good state with `Probe`
- then bootstrap runtime in a guided way
- then attach profiles and access methods
- then handle support and operations from the same bot

That makes it useful not just as a deploy-once tool, but as an ongoing control plane for a small self-hosted network setup.

## Project Layout

```text
app/backend/          Business API, authorization and durable worker scenarios
app/telegram_client/  aiogram client, RU/EN catalog and Rich Message presentation
app/db/               PostgreSQL adapter and schema helpers
app/services/         Shared controller update and release-maintenance helpers
rust/                 Central driver and node-local agent
runtime_assets/       VPN configuration and runtime adapters
scripts/   Install, healthcheck, update, rollback, and release helpers
tests/     Backend, aiogram, installation and runtime tests
```

Installation, environment configuration, updates, and maintenance commands are documented in [INSTALL.md](INSTALL.md).

## License

Apache-2.0. See [LICENSE](LICENSE).

## Safety

This project changes real system state during admin operations. `Bootstrap`, `Install Docker`, `Sync`, `Reinstall`, and related actions may install packages, write runtime files, manage Docker, and restart services on the nodes you connect.

Use it only on infrastructure you administer and review the deployment flow before running it in production.
