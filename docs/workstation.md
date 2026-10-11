[Русская версия](workstation.ru.md)

# Workstation

Workstation is the administrator's terminal interface. It connects to a Linux controller over SSH and uses the controller's backend for node, profile and settings operations.

## Install and start

Use the [README installation commands](../README.md#install-workstation), then run:

```sh
node-plane
```

Allow at least **70 columns × 30 rows**. Use a larger terminal for tables and long forms. To select a separate local data directory:

```sh
node-plane --state-dir /path/to/workstation-data
```

## Connect to a controller

Create an installation profile with a name, host/IP, SSH port and user. On first connection, verify the displayed host-key fingerprint against a trusted source. Enter the SSH password or use the available key-based access.

Workstation registers its public key during installation. When connecting to an existing controller, its key is bound to an administrator account. Choose the correct account if several administrators are offered. Local installation profiles describe connections; VPN profiles describe user access.

The header shows connection and loading status. SSH connections are reused across actions and close after inactivity. Deleting an installation profile removes its local saved connection and matching local host-key record; it does not remove Node Plane from the server.

## Navigation

- **↑ / ↓ / ← / →** move between controls; **Enter** activates the selected control.
- **Tab / Shift+Tab** move focus. Hovering the pointer changes focus; clicking activates.
- The mouse wheel scrolls long pages. Use the scrollbar to move through a page.
- **Back** returns to the parent menu. **Esc** backs out of a submenu or closes the current section; from the empty workspace it opens the exit confirmation.
- In the sidebar profile selector, **← / →** switches the installation profile while keeping focus on its name.
- A **×** marks an unavailable action. Controller sections become available after installation is confirmed.

## Sections

| Section | Purpose |
| --- | --- |
| Install Node Plane / Node Plane Settings | Install a controller; configure defaults, policies, monitoring, backups and reset/removal |
| Node Plane Updates | Check releases, choose a branch, update components and inspect results |
| Nodes | Search nodes by name/region/code, create a node, run setup, edit settings and diagnose operations |
| Access | Manage user profiles, access requests, grants and devices |
| Temporary configs | Create, retrieve and revoke short-lived configurations |
| Diagnostic | Check controller health and inspect recovery options |
| Settings | Local installation profiles, appearance, Workstation updates, connection/security and session information |

On startup the content area stays empty until you choose a section. If installation detection is unconfirmed, use **Check installation**.

## Install a controller

Choose **Install Node Plane**, fill the connection and installation fields, review the request and continue. Progress shows the current step and a log path. After completion, create a node in **Nodes** and run **Server setup**. Follow [Nodes](nodes.md) and [Access](access.md) for the remaining actions.

## Appearance and local files

Workstation stores its connection preferences, SSH material, logs and themes in the OS local data directory under `node-plane`, or the directory supplied by `--state-dir`. Keep this directory private.

Open **Settings → Appearance** to choose a theme or custom accent. Built-in theme files are created in `themes/` without overwriting existing files. Add a JSON file there and restart Workstation to load a custom theme:

```json
{
  "name": "My theme",
  "accent": "#55b1c2"
}
```

The optional `section_border` field sets decorative section borders separately from the accent. Use a short name and `#RRGGBB` colors. Invalid theme files are skipped with a warning.

## Update Workstation

Open **Settings → Workstation updates**. This updates the local application. **Node Plane Updates** updates the server installation.

For configurations, use **Copy URI** or **Save files** where offered. Telegram also provides QR delivery. See [Temporary configurations](temporary-configs.md) and [Troubleshooting](troubleshooting.md).
