[Русская версия](install.ru.md)

# Controller installation

Use this guide to install Node Plane directly on a Linux server. For installation from your computer, follow [Workstation](workstation.md).

## Requirements

- Linux with systemd and root/sudo access; Debian/Ubuntu for automatic Docker provisioning.
- Python 3.11 or 3.12, Git for release metadata, CA certificates and network access to GitHub and Telegram.
- A Telegram bot token and the numeric Telegram IDs of the initial administrators.
- PostgreSQL: an existing database or the installer's local Docker container.

The installer selects Python 3.12 or 3.11. Set `NODE_PLANE_PYTHON_BIN` to choose an interpreter. Keep the download directory outside `/opt/node-plane`.

## Download and install

Run on the server as root:

```sh
curl -fsSL https://raw.githubusercontent.com/saharoktyan/node-plane/main/scripts/controller_release.py -o /tmp/node-plane-controller-release.py
python3 /tmp/node-plane-controller-release.py download --branch main --destination /opt/node-plane-install
cd /opt/node-plane-install
./scripts/install.sh --mode simple
```

`--branch dev` selects a published development release; `--branch main` selects stable. Add `--ref vX.Y.Z` to the download command to select a specific tag. A download needs a published release with its artifacts.

Enter the bot token and administrator IDs when prompted. Leave `POSTGRES_DSN` empty to provision local PostgreSQL, or supply a DSN for an existing database. The installer prepares the Python environment, database schema, controller services and driver.

## Unattended installation

Create a private environment file using [`.env.example`](../.env.example). Fill `BOT_TOKEN` and `ADMIN_IDS`; use comma-separated numeric IDs. Include `POSTGRES_DSN` for an external database.

```sh
chmod 600 /root/node-plane.env
./scripts/install.sh --mode simple --non-interactive --env-file /root/node-plane.env
```

Keep the environment file private. Do not paste its contents into logs or support messages.

## Configuration reference

| Variable | Meaning |
| --- | --- |
| `BOT_TOKEN` | Telegram bot token |
| `ADMIN_IDS` | Initial administrators' numeric Telegram IDs |
| `POSTGRES_DSN` | External PostgreSQL connection; empty selects local provisioning |
| `NODE_PLANE_BASE_DIR` | Installation root; default `/opt/node-plane` |
| `NODE_PLANE_SHARED_DIR` | Persistent shared state; default `/opt/node-plane/shared` |
| `NODE_PLANE_UPDATE_BRANCH` | Release branch: `dev` or `main` |
| `SSH_KEY` | Controller's private SSH key for remote nodes |

The full template is [`.env.example`](../.env.example). Installer-generated values live in the shared environment. Changing connection settings does not move an existing database or installation directory.

## Check the installation

```sh
cd /opt/node-plane/current
./scripts/healthcheck.sh --mode simple
systemctl status node-plane-backend node-plane-telegram node-plane-driver --no-pager
systemctl status node-plane-backend-worker --no-pager
```

Open the bot from an administrator account and send `/start`. Create a [node](nodes.md), run its setup and [grant profile access](access.md).

## Quick Start

Administrators can open **Quick Start** from the bot's main menu. NPW shows progress to the right of the sidebar when no section is selected; press **F1** to open the guide from another section. NPW refreshes progress every second while the guide is visible and stops polling when you leave it.

The NPW guide checks server defaults, the first node, applied protocols and profile access. **Confirm installation defaults for servers** accepts values from **Install Node Plane → Basic → Installation defaults**. **Continue** opens the next step and reuses the selected profile before installation. Create user devices and obtain ordinary configurations in the Telegram bot, whose guide also checks issuance. Import the configuration into a VPN client and verify connectivity yourself.

After **Done**, this guide stays hidden for the administrator in that installation. NPW completion disables F1 and is separate from Telegram completion; the bot requires successful AWG or VLESS configuration issuance.

## Files and services

| Location | Contents |
| --- | --- |
| `/opt/node-plane/releases/` | Installed controller releases |
| `/opt/node-plane/current` | Active release |
| `/opt/node-plane/shared/` | Environment, database data, credentials, backups and journals |
| `/etc/node-plane/` | Local node configuration when a local agent is installed |

Services include `node-plane-backend`, `node-plane-telegram`, `node-plane-driver` and the worker service. Each managed node has its own agent. Only one local node can use the controller's local agent.

Use [Maintenance](maintenance.md) for updates, reset and removal. See [Troubleshooting](troubleshooting.md) when installation or health checks fail.
