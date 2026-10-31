[Русская версия](README.ru.md)

# Node Plane

**Node Plane is an open-source control plane for self-hosted VPN infrastructure.** It helps people who run their own Linux VPS install and manage VLESS/Xray REALITY and AmneziaWG on one or several servers. Administrators use a local terminal application (Workstation) and a Telegram bot; VPN configurations are delivered to users through Telegram.

**Early public beta:** I'm looking for users who can install Node Plane on a disposable VPS and try it in real-world conditions. Published prereleases currently use the `dev` channel as a temporary distribution channel; a separate public `beta` channel is [planned](docs/roadmap.md#public-beta-preparation). The commands below use the current development channel.

## Why Node Plane

Start with a Linux VPS and SSH access. Workstation installs the controller from your computer, so you do not need to open a server console or run installation commands there. A single VPS can host both the controller and VPN protocols; you can add remote servers later.

- **One setup workflow:** connect, install Node Plane, then follow Quick Start to deploy the selected VPN protocols.
- **Administration from your computer and phone:** manage servers, user access, updates, backups and diagnostics through Workstation or Telegram.
- **No public web admin panel required:** Workstation connects through SSH; node agents use mTLS. This reduces the interfaces you need to expose, but does not guarantee security or prevent IP blocking.
- **Designed for personal and small-group VPNs:** create profiles, grant server/protocol access, issue configurations and manage expiry or revocation.

Node Plane is aimed at people who want to run their own VPN with less manual server administration, especially when they need to manage more than one server or share access with other people. Its current tradeoffs are fewer protocol-management features than established panels, no browser administrator interface, and configuration delivery to users primarily through Telegram.

## Beta testing

I've tested the core installation and administration workflows myself. With this public beta, I want to learn how Node Plane behaves on different VPS providers, operating systems, networks and client devices, and which improvements would be most valuable to you.

Useful beta scenarios include:

- installing on a fresh Debian or Ubuntu VPS;
- using Workstation on Linux, macOS or Windows;
- deploying VLESS/REALITY and AmneziaWG on local and remote nodes;
- importing configurations on your usual VPN clients and networks;
- inviting another user, granting access and retrieving a configuration through Telegram;
- changing protocol and access settings, testing expiry, freezing and revocation;
- updating the Workstation and controller installation, then checking that users and settings remain available;
- testing backups, diagnostics and recovery after service or node restarts.

Please report what was confusing or failed, which OS, VPS provider, protocol and VPN client you used, and the exact reproduction steps. Please also share which planned improvements matter most to you, what would make you keep using Node Plane, and what might make you stop. Redact tokens, private keys, VPN configurations and other credentials. The [GitHub Issues](https://github.com/saharoktyan/node-plane/issues) page is the preferred feedback channel; feature ideas and usability feedback are welcome too.

## Quick start

You need a **Linux x64 VPS with systemd**, privileged SSH access, a Telegram bot token from [@BotFather](https://t.me/BotFather), and your numeric Telegram user ID. Debian/Ubuntu support automatic Docker installation. Workstation runs on Linux x64, macOS Intel/Apple Silicon and Windows x64.

### Install Workstation

Run this **on your own computer**.

**Linux / macOS**

```sh
curl -fsSL https://raw.githubusercontent.com/saharoktyan/node-plane/dev/scripts/install_workstation.sh | bash -s -- --channel dev
```

**Windows — PowerShell**

```powershell
& ([scriptblock]::Create((irm 'https://raw.githubusercontent.com/saharoktyan/node-plane/dev/scripts/install_workstation.ps1'))) -Channel dev
```

Open a new terminal and run `node-plane-dev`. On Windows you can also open **Node Plane (Dev)** from Start. Allow a terminal of at least 70 columns × 30 rows. [Build from source](docs/workstation.md#build-from-source).

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

Traffic statistics are available; traffic quotas and connection-count enforcement are not implemented yet. Revoking VLESS access blocks new connections, while established sessions may remain until disconnect.

## Compared with 3x-ui and Remnawave

This compares documented standard workflows, not every third-party integration or customized deployment. Checked against official documentation on **2026-10-30**; these are feature comparisons, not comparative security or performance tests.

| Area | Node Plane today | 3x-ui | Remnawave |
| --- | --- | --- | --- |
| Initial installation | Workstation deploys over SSH from your computer | Server installer/Docker; unattended and cloud-init options | Panel, reverse proxy and Node setup |
| Administration | Workstation TUI and Telegram bot | Web panel and Telegram bot | Web panel; Telegram notifications and ecosystem bots |
| Multiple servers | Controller with node agents | Master manages other panel instances over API | Panel with Remnawave Nodes |
| Protocol scope | VLESS REALITY TCP/XHTTP and AmneziaWG | Broader protocol selection, including AmneziaWG | Xray-based proxy management |
| Configuration subscriptions | Planned | Available | Available |
| User/client traffic quotas | Planned | Available | Available |
| Access expiry | Available; established VLESS-session caveat above | Available | Available |

Sources: [3x-ui features](https://docs.sanaei.dev/docs/), [installation](https://docs.sanaei.dev/docs/guide/installation/), [multi-node](https://docs.sanaei.dev/docs/operations/multi-node/); Remnawave [setup](https://docs.rw/overview/quick-start/), [users and limits](https://docs.rw/learn-en/users/), [Telegram integrations](https://docs.rw/awesome-remnawave/).

Choose Node Plane for its workstation-led setup and integrated administration workflow. If you need subscriptions, traffic quotas or a browser panel today, the alternatives already provide those capabilities. A web panel can also be kept private: for example, 3x-ui documents [localhost binding and SSH tunneling](https://docs.sanaei.dev/docs/guide/update-uninstall/).

## Planned VPN improvements — not implemented

These are development priorities for the beta period, not a guarantee that every item ships before stable 1.0. See the [roadmap](docs/roadmap.md#2-existing-xray-and-amneziawg-capabilities).

- **Xray/AWG subscriptions:** renewable configuration delivery, with actual import/refresh tested in declared clients.
- **Monthly traffic quotas per account/VPN profile:** accounting scope, reset periods and enforcement still need implementation.
- **Concurrent connection limits per account/profile:** define and test protocol-specific session/activity semantics; this is separate from configured device counts or IP counts.
- **Speed limits per profile on each node:** separate upload/download ceilings shared by all of that profile's devices and flows on the node; protocol-specific enforcement still needs validation and implementation.
- **Basic server chains:** configure entry, optional intermediate servers and exit through Workstation/Telegram.
- **Active VLESS-session revocation:** evaluate termination without affecting other users or shared listeners.

Per-IP byte quotas, global cross-node speed budgets and bandwidth-sharing policies remain separate design decisions. Chain self-healing is deferred pending demand. Grafana/Loki, Vaultwarden, a client SDK, custom roles and service modules are longer-term topics, not current features.

## Requirements and documentation

For a small combined controller/VPN installation, budget roughly **1 vCPU, 1 GiB RAM and 10 GiB free disk** after the OS, with headroom for traffic, logs and backups. This is a sizing guideline, not a throughput guarantee. The controller uses PostgreSQL and systemd; standard deployments use Docker for PostgreSQL and VPN runtimes. VPN client ports must be reachable.

[Workstation](docs/workstation.md) · [Telegram](docs/telegram.md) · [Nodes](docs/nodes.md) · [Protocols](docs/protocols.md) · [Maintenance](docs/maintenance.md) · [Troubleshooting](docs/troubleshooting.md) · [All documentation](docs/README.md)

Report reproducible problems through [GitHub Issues](https://github.com/saharoktyan/node-plane/issues), including version, OS, steps and redacted diagnostics. Never include bot tokens, private keys or VPN configurations. Current priorities: [Roadmap](docs/roadmap.md). Development: [Architecture](docs/development/architecture.md) and [Testing](docs/development/testing.md). License: [Apache-2.0](LICENSE).
