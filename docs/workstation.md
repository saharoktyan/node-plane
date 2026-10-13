[Русская версия](workstation.ru.md)

# Workstation

Workstation is the administrator's terminal interface. It connects to a Linux controller over SSH and uses the controller's backend for node, profile and settings operations.

## Install and start

Use the [README installation commands](../README.md#install-workstation), then run:

```sh
node-plane-dev
```

Allow at least **70 columns × 30 rows**. Use a larger terminal for tables and long forms. To select a separate local data directory:

```sh
node-plane-dev --state-dir /path/to/workstation-data
```

For stable, run `node-plane-stable`. On Windows, the installer adds **Node Plane Workstation (dev)** or **Node Plane Workstation (stable)** to the Start menu. Find it through Windows Search to open Workstation in a terminal. Updating preserves the shortcut; uninstalling Workstation removes it.

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

## Installation diagnostics

Diagnostic first checks the controller over SSH, including services, PostgreSQL and published schema checksums. When the backend is reachable, it checks registered servers in batches: agents, selected protocol containers/configuration structure, versions and host resources. Results from every page are included in the final scrollable report. Missing or incomplete server coverage produces a warning; controller-only results do not certify the entire installation. Recovery of a stopped controller service requires a separate confirmation.

The **Offer service and operation recovery** option retains its last selection across Workstation sessions. Each recovery action has its own confirmation.

## Sections

| Section | Purpose |
| --- | --- |
| Install Node Plane / Node Plane Settings | Install a controller; configure defaults, policies, monitoring, backups and reset/removal |
| Node Plane Updates | Check releases, choose a branch, update the installation and inspect results or failed nodes |
| Nodes | Search nodes by name/region/code, create a node, run setup, edit settings and diagnose operations |
| Access | Manage user profiles, access requests and grants |
| Temporary configs | Create, retrieve and revoke short-lived configurations |
| Diagnostic | Check controller services/schema and all registered servers; inspect recovery options |
| Settings | Local installation profiles, appearance, Workstation updates, connection/security and session information |

On startup Quick Start appears beside the sidebar. In NPW it ends after installing the node protocols; create user devices and obtain ordinary configurations in the Telegram bot. Select **Done** after setup or confirm **Dismiss** to hide the guide permanently. Removing nodes does not restore it, and F1 no longer opens it. If installation detection is unconfirmed, use **Check installation**.

Automatic check, backup and alert preferences appear when the corresponding feature is enabled. Select an interval directly; backups also offer a retention count. **Node Plane Settings → Basic** contains installation defaults and general controller options.

In profile access settings, **Duration → Custom duration** accepts whole-number values with H (hours), D (days), M (30 days) or Y (365 days), such as `12H` or `1M`. User devices are managed by their owner in the Telegram bot. Server **Maintenance** groups diagnostics, reinstall and reset/removal; diagnostics also check configured TCP and UDP ports.

## Install a controller

Choose **Install Node Plane**, fill the connection and installation fields, review the request and continue. Progress shows the current step and a log path. After completion, create a node in **Nodes** and run **Server setup**. Follow [Nodes](nodes.md) and [Access](access.md) for the remaining actions.

## Remove a controller

Open **Node Plane Settings → Reset & removal**, choose whether to uninstall nodes too, and confirm the removal phrase. Progress refreshes automatically. During preparation, Node Plane Settings shows **Finish removal** and **Cancel removal**; finishing becomes available once node cleanup is complete. Cancellation keeps the controller and stops further preparation after the current step. It cannot restore nodes already removed. After confirming **Finish removal**, Workstation verifies the uninstall and shows the goodbye screen; cancellation is no longer available.

## Appearance and local files

Workstation stores its connection preferences, SSH material, logs and themes in the OS local data directory under `node-plane-dev` for dev and `node-plane` for stable, or the directory supplied by `--state-dir`. Keep this directory private.

Open **Settings → Appearance** to choose a theme or custom accent. Built-in theme files are created in `themes/` without overwriting existing files. Add a JSON file there and restart Workstation to load a custom theme:

```json
{
  "name": "My theme",
  "accent": "#55b1c2"
}
```

The optional `section_border` field sets decorative section borders separately from the accent. Use a short name and `#RRGGBB` colors. Invalid theme files are skipped with a warning.

## Stable and dev channels

Dev NPW installs and manages dev controllers; stable NPW manages stable controllers. The form channel is fixed by the build. Management checks the installed controller version: changing the update preference alone does not unlock access. Installation detection and diagnostics without recovery remain available.

Installers use separate `$HOME/.local/share/node-plane-workstation/dev` and `stable` directories, updater receipts and launch commands. Dev uses the local data directory `node-plane-dev`; stable uses `node-plane`. Each NPW only updates within its own channel.

To transition:

1. In the source NPW open **Settings → Profiles**, select the profile and choose **Copy to stable NPW** or **Copy to dev NPW**.
2. Open or restart the target NPW. Connection metadata and the selected account are copied; the original profile stays. SSH keys, host fingerprints and operation journals are not copied: verify the server fingerprint and provide SSH access again if needed.
3. In **Node Plane Updates**, confirm switching the controller to this NPW's channel, select a release and update the installation. Until the runtime is updated, only the transition is available; ordinary management is blocked.

Copying uses the target NPW's standard local data directory and refuses conflicting profiles. Transition requires a published target-channel release. Copying a profile does not reinstall the controller or move its database.

## Update Workstation

Open **Settings → Workstation updates**. This updates the local application. **Node Plane Updates** updates the server installation.

Workstation checks for updates at startup and offers an available version after any connection check finishes. You can decline and return to **Settings → Workstation updates** later.

After a successful local update, select **Restart** to open the updated Workstation. Other actions remain unavailable until restart.

For configurations, use **Copy URI** or **Save files** where offered. Telegram also provides QR delivery. See [Temporary configurations](temporary-configs.md) and [Troubleshooting](troubleshooting.md).

Open **Settings → Documentation** to select a named guide with a short description; Enter or click opens its GitHub page in your browser. For a local node, entering `.` as its public address uses the controller address saved in the selected installation profile.
