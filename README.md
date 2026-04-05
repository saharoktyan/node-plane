# Node Plane

Node Plane is a Telegram-based control plane for self-hosted secure connectivity.

It is built for operators who want one interface for node setup, runtime bootstrap, profile management, access delivery, and day-to-day operations across infrastructure they control themselves.

Instead of juggling shell scripts, scattered configs, and ad-hoc server notes, you manage the full lifecycle from a Telegram admin flow: register a node, validate it with `Probe`, deploy runtime with `Bootstrap`, create profiles, and deliver connection configs to users.

The recommended way to deploy Node Plane is through the bundled `install.sh` workflow, which prepares the runtime for either `Simple Mode` or `Portable Mode`.

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
- includes diagnostics, telemetry, updates, rollback, and release cleanup
- supports Russian and English UI

## Deployment Modes

### Simple Mode

Use this when you want the shortest path to a working deployment.

- bot runs directly on the host
- intended for `systemd` + Python venv setup
- supports same-host runtime deployment
- can also manage additional remote nodes over `ssh`
- best fit for a single VPS

### Portable Mode

Use this when the bot should manage remote nodes over SSH.

- bot runs in Docker via `docker compose`
- nodes are managed remotely over `ssh`
- runtime images are pulled from `ghcr.io/seventh7dev/node-plane`
- better fit for multi-node setups

Important constraint:
`local` node deployment is supported only in `Simple Mode`. If the bot runs inside Docker, managed nodes should be added via `ssh`.

## Feature Matrix

| Capability | Simple Mode | Portable Mode |
| --- | --- | --- |
| Bot runtime | Host + `systemd` | Docker + `docker compose` |
| Same-host `local` node | Yes | No |
| Remote `ssh` nodes | Yes | Yes |
| Single-server setup | Excellent fit | Possible, but not the main target |
| Multi-node setup | Good fit | Excellent fit |
| Best for | Fastest self-hosted start | Separated bot host and remote node fleet |

## Supported Runtime

- `VLESS` over `Xray Reality`
  - transports: `tcp`, `xhttp`
- `AmneziaWG`

Current upstream images used by the project:

- Xray: `ghcr.io/xtls/xray-core:25.12.8`
- AWG: `amneziavpn/amneziawg-go:0.2.16`

For AWG nodes, Node Plane builds and deploys its own wrapper image during bootstrap.

## Main Workflow

1. Deploy the bot in `Simple Mode` or `Portable Mode`.
2. Open the bot from the Telegram admin account.
3. Send `/start` and create the first managed server.
4. Run `Probe` to validate host readiness.
5. Run `Bootstrap` to install and configure runtime.
6. Create one or more profiles.
7. Let users request or receive connection configs through the bot.
8. Use sync, diagnostics, telemetry, update, and rollback flows for ongoing operations.

## Quick Start

### Simple Mode

```bash
git clone https://github.com/seventh7dev/node-plane.git node-plane-src
cd node-plane-src
./scripts/install.sh --mode simple
```

Then follow the full guide in [INSTALL.md](INSTALL.md).

### Portable Mode

```bash
git clone https://github.com/seventh7dev/node-plane.git node-plane-src
cd node-plane-src
./scripts/install.sh --mode portable
```

Then follow the full guide in [INSTALL.md](INSTALL.md).

If you prefer SSH for cloning, configure a GitHub SSH key on the host first and then use:

```bash
git clone git@github.com:seventh7dev/node-plane.git node-plane-src
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
- provide `AWG` direct links, QR, and `.conf` fallback

### Operations And Maintenance

- telemetry and traffic usage reporting
- health checks and diagnostics
- scripted updates with rollback support
- automatic Docker and PostgreSQL runtime provisioning during install/update
- release cleanup helpers

## Operator Experience

Node Plane is opinionated about the actual workflow operators go through:

- first bring a node into a known-good state with `Probe`
- then bootstrap runtime in a guided way
- then attach profiles and access methods
- then handle support and operations from the same bot

That makes it useful not just as a deploy-once tool, but as an ongoing control plane for a small self-hosted network setup.

## Project Layout

```text
app/       Bot code, handlers, services, storage, and runtime integration
scripts/   Install, healthcheck, update, rollback, and release helpers
tests/     Unit tests for bot flows, migration, and runtime behavior
```

Installation, environment configuration, updates, and maintenance commands are documented in [INSTALL.md](INSTALL.md).

## License

Apache-2.0. See [LICENSE](LICENSE).

## Safety

This project changes real system state during admin operations. `Bootstrap`, `Install Docker`, `Sync`, `Reinstall`, and related actions may install packages, write runtime files, manage Docker, and restart services on the nodes you connect.

Use it only on infrastructure you administer and review the deployment flow before running it in production.
