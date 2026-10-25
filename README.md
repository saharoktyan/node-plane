[Русская версия](README.ru.md)

# Node Plane

Node Plane is an open-source control plane for self-hosted VPN infrastructure. Use Workstation on your computer or the Telegram bot to set up servers, manage access, deliver configurations and maintain your installation.

A Linux controller stores accounts and settings in PostgreSQL and coordinates node agents. A VPN node can run on the controller or on a separate Linux server. Supported protocols are VLESS with Xray REALITY over TCP/XHTTP and AmneziaWG.

## Install Workstation

Workstation is the administrator's terminal interface. Install it on your own computer, then run `node-plane`.

**Linux (x64) and macOS (Intel or Apple Silicon)**

```sh
curl -fsSL https://raw.githubusercontent.com/saharoktyan/node-plane/dev/scripts/install_workstation.sh | bash
```

**Windows (x64), PowerShell**

```powershell
irm https://raw.githubusercontent.com/saharoktyan/node-plane/dev/scripts/install_workstation.ps1 | iex
```

### Development builds

Install the latest published alpha and run `node-plane-dev`:

```sh
curl -fsSL https://raw.githubusercontent.com/saharoktyan/node-plane/dev/scripts/install_workstation.sh | bash -s -- --channel dev
```

```powershell
& ([scriptblock]::Create((irm 'https://raw.githubusercontent.com/saharoktyan/node-plane/dev/scripts/install_workstation.ps1'))) -Channel dev
```

To select an exact alpha, add `--tag vX.Y.Z-alpha.N` or `-Tag 'vX.Y.Z-alpha.N'`. These commands install published binaries, including GitHub prereleases. For the current `dev` branch or a tag without published binaries, [build from source](docs/workstation.md#build-from-source).

Workstation requires a supported OS, a terminal of at least 70 columns × 30 rows and SSH access to the controller. Open a new terminal if the command is not yet on PATH. Initial controller installation requires root access on the target server.

## Set up your installation

Prepare a Linux server with systemd, root access, a Telegram bot token and your numeric Telegram ID. Debian/Ubuntu hosts support automatic Docker installation. The controller runs as systemd services and uses PostgreSQL. The standard installation runs PostgreSQL and the VPN protocols in Docker; an existing PostgreSQL server can also be used.

1. Create an installation profile in Workstation and connect to the server.
2. Install Node Plane with your bot token and administrator Telegram ID.
3. Create a local or SSH node and run **Server setup**.
4. Create or approve a user profile and grant access.
5. Open the bot with `/start` to get a configuration.

You can also [install the controller directly on the server](docs/install.md).

## Server requirements

Use a Linux x64 server with systemd. Debian/Ubuntu support automatic Docker installation. Initial setup requires root or passwordless sudo access. VPN nodes need a public client address and reachable protocol ports.

Estimated minimum resources for a small installation using published binaries:

| Server role | CPU | RAM | Free disk space |
| --- | --- | --- | --- |
| Controller with local PostgreSQL | 1 vCPU | 1 GiB | 5 GiB |
| Agent with VLESS and AmneziaWG | 1 vCPU | 1 GiB | 5 GiB |
| Controller, local PostgreSQL, agent and both protocols | 1 vCPU | 1 GiB | 10 GiB |

Disk figures are available space after installing the OS. Allow additional space for database growth, logs and backups. CPU and bandwidth needs grow with concurrent VPN traffic. The combined installation has been observed to run on a 1 GiB server, using about 350 MiB at idle. Plan additional RAM for active traffic; the other figures are sizing estimates.

## Guides

| Task | Guide |
| --- | --- |
| Install the controller manually | [Installation](docs/install.md) |
| Use the terminal interface | [Workstation](docs/workstation.md) |
| Use the bot | [Telegram](docs/telegram.md) |
| Create and maintain VPN nodes | [Nodes](docs/nodes.md) |
| Manage profiles, devices and requests | [Access](docs/access.md) |
| Configure protocols | [Protocols](docs/protocols.md) |
| Issue short-lived configurations | [Temporary configurations](docs/temporary-configs.md) |
| Configure installation defaults and policies | [Settings](docs/settings.md) |
| Update, back up and diagnose the installation | [Maintenance](docs/maintenance.md) |
| Resolve a problem | [Troubleshooting](docs/troubleshooting.md) |

## Development

Start with the [documentation index](docs/README.md), [architecture](docs/development/architecture.md) and [development/testing guide](docs/development/testing.md). Planned work is in the [roadmap](docs/roadmap.md); released changes are in [CHANGELOG.md](CHANGELOG.md).

VPN configurations, database backups and SSH keys contain secrets. Store them privately. Expiry or revocation blocks new VLESS connections; an established connection may continue until it disconnects.

See [LICENSE](LICENSE) for the project license.
