[Русская версия](README.ru.md)

# Node Plane

**Node Plane is an open-source control plane for self-hosted VPN infrastructure.** It helps people who run their own Linux VPS install and manage VLESS/Xray REALITY and AmneziaWG on one or several servers. Administrators use a local terminal application (Workstation) and a Telegram bot; VPN configurations are delivered to users through Telegram.

**Early public beta:** I am looking for users who can install Node Plane on a test VPS and try it in real conditions. Public `0.x` versions use the separate `beta` channel; experimental alpha builds stay in `dev`. The stable 1.0 feature cut will follow beta feedback.

## Why Node Plane

Start with a Linux VPS and SSH access. Workstation installs the controller from your computer, so you do not need to open a server console or run installation commands there. A single VPS can host both the controller and VPN protocols; you can add remote servers later.

- **One setup workflow:** connect, install Node Plane, then follow Quick Start to deploy the selected VPN protocols.
- **Administration from your computer and phone:** manage servers, user access, updates, backups and diagnostics through Workstation or Telegram.
- **No public web admin port required:** Workstation connects through SSH; node agents use mTLS. This reduces the interfaces you need to expose, but does not guarantee security or prevent IP blocking.
- **Access that fits small groups:** create profiles, approve access requests, grant server/protocol access, issue temporary configurations and manage expiry or revocation.
- **Telegram delivery:** users can receive configuration files, links and QR codes through a localized bot.

Node Plane is aimed at people who want to run their own VPN with less manual server administration, especially when they need to manage more than one server or share access with other people. Its current tradeoffs are fewer protocol-management features than established panels, no browser administrator interface, and configuration delivery to users primarily through Telegram.

## Beta testing

I've tested the core installation and administration workflows myself. For the public beta, the minimum request is simple: install Node Plane on a test VPS and report what worked or failed. I want to learn how it behaves across VPS providers, operating systems, networks and client devices, and which improvements should come next.

If you want to go further, try the Workstation and Telegram workflows, local and remote nodes, protocol changes, access approval, expiry, freezing, revocation, updates, backups and recovery. For every report, include the OS, VPS provider, protocol, VPN client and exact reproduction steps when something fails or feels confusing. Please also say which missing features matter most to you and what would make you keep using Node Plane or stop. Redact tokens, private keys, VPN configurations and other credentials. The [GitHub Issues](https://github.com/saharoktyan/node-plane/issues) page is the preferred feedback channel.

As an informal usability check, two people with no Linux experience started with two empty VPSs and, in about 20 minutes, completed the full installation path and ended up with two VPN servers on each VPS, with VLESS TCP, VLESS XHTTP and AmneziaWG configured. This is one anecdotal observation, not a benchmark or a guarantee for every environment.

## Quick start

You need a **Linux x64 VPS with systemd**, privileged SSH access, a Telegram bot token from [@BotFather](https://t.me/BotFather), and your numeric Telegram user ID. Debian/Ubuntu support automatic Docker installation. Workstation runs on Linux x64, macOS Intel/Apple Silicon and Windows x64.

### Install Workstation

Run this **on your own computer**.

**Linux / macOS**

```sh
curl -fsSL https://raw.githubusercontent.com/saharoktyan/node-plane/beta/scripts/install_workstation.sh | bash -s -- --channel beta
```

This command downloads a shell bootstrap from GitHub and executes it immediately. The script is short and open for inspection; if you want to review it first, download it and run the local copy instead. The bootstrap discovers a release and downloads the Workstation installer from GitHub Releases over HTTPS. Check the release and installer behavior against the published project files before running it; the command itself is not a substitute for reviewing the source.

**Windows — PowerShell**

```powershell
& ([scriptblock]::Create((irm 'https://raw.githubusercontent.com/saharoktyan/node-plane/beta/scripts/install_workstation.ps1'))) -Channel beta
```

Open a new terminal and run `node-plane-beta`. On Windows you can also open **Node Plane (Beta)** from Start. Allow a terminal of at least 70 columns × 30 rows. [Build from source](docs/workstation.md#build-from-source).

### Get connected

1. Create an installation profile in Workstation with your server IP, SSH port and user. Connect using a password or an existing SSH key; verify the displayed host fingerprint.
2. Choose **Install Node Plane**, provide the bot token and your Telegram ID, and confirm installation.
3. Follow **Quick Start** to create a node, choose protocols and run server setup. Choose a local node to use the same VPS, or an SSH node for another server.
4. Open your bot, send `/start`, choose **Get config**, and import the configuration into a compatible VPN client.

Your installation Telegram ID receives administrator access automatically. To share access with someone else, approve their request or create their profile and grant server access. See [Profiles and access](docs/access.md).

## What works today

- Local and remote VPN nodes, protocol deployment and server settings.
- Accounts, VPN profiles, server/protocol grants, access requests and AWG devices.
- Configuration files, links and QR codes delivered through Telegram.
- Access expiry, profile freezing, revocation and temporary configurations.
- Traffic statistics, host observations, notifications and alerts.
- Installation updates, configuration backups/restore and operation diagnostics/recovery.

VLESS configurations are delivered as URIs or QR codes, and AmneziaWG configurations as `.conf` files. Use a VPN client that supports the relevant format; a VLESS URI configures the proxy connection, while full-device routing requires a client with TUN/VPN mode. I have tested VLESS with v2rayA on Linux, v2rayN on Windows, NekoBox on Android (TCP transport only) and v2box on iOS. I have also tested AmneziaWG with AmneziaVPN on Linux, Windows, Android and iOS. These are maintainer test results, not a compatibility guarantee for every version or device.

Traffic statistics are available; traffic quotas and connection-count enforcement are not implemented yet. Revoking VLESS access blocks new connections, while established sessions may remain until disconnect.

## Compared with 3x-ui and Remnawave

This compares documented standard workflows, not every third-party integration or customized deployment. Checked against official documentation on **2026-10-30**; these are feature comparisons, not comparative security or performance tests.

| Area | Node Plane today | 3x-ui | Remnawave |
| --- | --- | --- | --- |
| Initial installation | Workstation deploys over SSH from your computer; no manual VPS-console commands required | Server installer/Docker; unattended and cloud-init options | Panel, reverse proxy and Node setup |
| Public web administration port | Not required for the standard workflow | Web administration is part of the documented workflow; it can be restricted, for example with localhost binding and SSH tunneling | Browser-based Panel/Node administration |
| Administration | Workstation TUI and Telegram bot | Web panel and Telegram bot | Web panel; Telegram notifications and ecosystem bots |
| Multiple servers | One controller manages local and remote node agents | Master manages other panel instances over API | Panel with Remnawave Nodes |
| Profiles and access approval | Profiles, grants, access requests, approval, expiry, freezing, revocation and temporary configurations | User and client management through the panel and documented integrations | User management, traffic/access controls and ecosystem integrations |
| Configuration delivery | Telegram files, links and QR codes; localized bot flows | Panel and documented bot/integration workflows | Panel and ecosystem integrations |
| Protocol scope | VLESS REALITY TCP/XHTTP and AmneziaWG | Broader protocol selection, including AmneziaWG | Xray-based proxy management |
| Configuration subscriptions | Planned | Available | Available |
| User/client traffic quotas | Planned | Available | Available |
| Access expiry | Available; established VLESS-session caveat above | Available | Available |

Sources: [3x-ui features](https://docs.sanaei.dev/docs/), [installation](https://docs.sanaei.dev/docs/guide/installation/), [multi-node](https://docs.sanaei.dev/docs/operations/multi-node/); Remnawave [setup](https://docs.rw/overview/quick-start/), [users and limits](https://docs.rw/learn-en/users/), [Telegram integrations](https://docs.rw/awesome-remnawave/).

Node Plane's strengths are the workstation-led installation, no required public admin port, approval-based access model, temporary configurations and Telegram delivery. The alternatives offer broader protocol or subscription/quota capabilities today and browser-based administration. Choose based on which workflow fits your environment; a web panel can also be kept private, as 3x-ui documents with [localhost binding and SSH tunneling](https://docs.sanaei.dev/docs/guide/update-uninstall/).

## Planned VPN improvements — not implemented

These are development priorities for the beta period, not a guarantee that every item ships before stable 1.0. See the [roadmap](docs/roadmap.md#near-term-development-priorities).

- Renewable subscriptions for easier configuration refresh.
- Traffic quotas and connection limits.
- Per-profile speed limits.
- Basic server chains and stronger active-session revocation.

## Requirements and documentation

For a small combined controller/VPN installation, budget roughly **1 vCPU, 1 GiB RAM and 10 GiB free disk** after the OS, with headroom for traffic, logs and backups. This is a sizing guideline, not a throughput guarantee. The controller uses PostgreSQL and systemd; standard deployments use Docker for PostgreSQL and VPN runtimes. VPN client ports must be reachable.

[Workstation](docs/workstation.md) · [Telegram](docs/telegram.md) · [Nodes](docs/nodes.md) · [Protocols](docs/protocols.md) · [Maintenance](docs/maintenance.md) · [Troubleshooting](docs/troubleshooting.md) · [All documentation](docs/README.md)

Report reproducible problems through [GitHub Issues](https://github.com/saharoktyan/node-plane/issues), including version, OS, steps and redacted diagnostics. Never include bot tokens, private keys or VPN configurations. Current priorities: [Roadmap](docs/roadmap.md). Development: [Architecture](docs/development/architecture.md) and [Testing](docs/development/testing.md). License: [Apache-2.0](LICENSE).
