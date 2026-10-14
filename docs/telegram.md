[Русская версия](telegram.ru.md)

# Telegram bot

The bot provides user configuration delivery and administrator controls for the same Node Plane installation managed by Workstation.

## First start

Open your installed bot and send `/start`. The Telegram IDs supplied during installation receive administrator access. Other users can request access according to the installation's request policy.

The navigation line at the top shows your location. Click a parent item to return to that screen. Long paths are shortened to fit the interface. **Back** returns one level; a confirmation protects unsaved server/profile drafts.

## User actions

- **Get config:** choose a server, protocol and Xray transport when choices are available. On your first AWG request, enter a name for your device. Access approval alone does not create a device. When you have several devices, choose one. The preparation screen refreshes automatically while the device and configuration are being prepared; Back returns to the server list. Download the file, use the URI or scan the QR with a compatible VPN client.
- **Profile:** view access status, expiry, server count and total traffic. **Details** opens account information, configuration statistics and the server/protocol breakdown.
- **Settings:** change available personal preferences.
- **Notifications:** read administrator announcements. This section is hidden when empty. **Close** closes the message; **Dismiss** also removes it from your notification list.

Use the separate-message action when an iOS client cannot copy a link or public key from the formatted screen. Treat delivered configurations as private credentials.

## Administrator actions

The administrator menu provides servers, profiles, access requests, updates, settings and announcements. Start with these guides:

1. [Create a node and run setup](nodes.md).
2. [Approve requests and grant access](access.md).
3. [Configure installation defaults and policies](settings.md).
4. [Update, back up and inspect alerts](maintenance.md).

Overview shows server/profile counts and adds rows for queued, running or blocked tasks when present. Pending requests appear when request intake is enabled and the queue is nonempty. Alerts appears when monitoring is enabled and active alerts remain undismissed. Both lists return directly to the administrator menu. The last check time and Refresh link are below the table. Updates and Backups are in the administrator menu’s System section. Settings contains controller diagnostics, recovery, monitoring preferences and a Documentation text link to individual GitHub guides. The `/status` command opens Overview.

In a server card, **Probe** reads the agent and Docker state, selected protocol containers and configuration structure, configured TCP/UDP ports, host resources and agent/runtime versions. The result includes its observation time and response duration. Missing measurements are unknown; unselected protocols are omitted. Probe does not apply settings or verify a VPN connection from a client.

Temporary configurations are available in administrator settings. They have their own list and revocation actions; see [Temporary configurations](temporary-configs.md).

## Diagnostics

Controller diagnostics checks release files, environment keys, PostgreSQL, published migration checksums, systemd services, backend readiness and blocked maintenance jobs. **Server diagnostics** opens live checks for five servers per page: agent connectivity/version, Docker, selected protocol containers/configuration structure, pending settings, runtime version and host resource pressure. A failure on one server preserves results for the others. Disabled servers are marked as skipped. Each page states how many registered servers it covers; it is not a fleet-wide health certificate. Open a check for its explanation or the server card for recovery.

These checks inspect local services and configuration structure. A real VPN client connection remains a separate end-to-end test.

## Operation progress

Active setup, update, deletion and cleanup screens refresh their progress. Leaving the screen does not cancel an accepted operation. Reopen its status to check the outcome. If a job is blocked, inspect its reason and diagnostics before starting another mutation.

Update, alert and recovery notifications have a Close action. Alerts retain their separate Inbox; Dismiss remains available in the alert list. Closing a notification preserves the menu you were using. Dismissing alert attention does not repair the underlying condition.
