[Русская версия](maintenance.ru.md)

# Maintenance

Use the controller settings and updates screens in either interface. Read confirmations before operations that change access, restore data or remove resources.

## Updates

Workstation: **Node Plane Updates**. Telegram: administrator **Updates**.

The screen shows the branch, installed version, component state, latest known release and last check time. Run **Check** to query releases. Enable **Auto checks** and choose **1 hour**, **6 hours** or **1 day**; frequency controls appear while automatic checks are enabled. Administrators receive a notification when a new release is found.

Review an update and start it. Progress shows component steps. **Update results** appears when a completed result is available; Dismiss removes its shortcut from Updates. Failed node updates are shown separately, with links to their server cards for diagnostics and setup recovery. Changing branch or rolling back requires a separate confirmation because it can break installation compatibility.

A controller update can restart the bot. Reopen Updates if progress is interrupted. A confirmed rollback ends with `rolled_back` and releases the update lock; fix the cause and start a new update. If the outcome is uncertain, use Operations before attempting another update.

For a direct server check:

```sh
cd /opt/node-plane/current
./scripts/healthcheck.sh --mode simple
```

The healthcheck detects the installation root when run through `current` or from `releases/<release>`, reads `shared/.env`, and checks the installed `.venv`. A uv-managed environment does not need pip installed inside it. Docker and TUN availability are also reported as prerequisites for running a VPN node on this host; missing local VPN prerequisites alone do not make a controller with remote nodes unhealthy.

## Backups

Workstation: **Node Plane Settings → Backups → Backups list → Download backup** saves a snapshot to a local JSON file. **Import backup from file** adds a validated snapshot to the selected controller's catalog without restoring it. Open the imported entry and choose Restore separately. Downloads refuse to overwrite existing files; imports validate the checksum and deduplicate identical snapshots. Limit: 64 MiB. The HTTP API provides authenticated export/import endpoints for the same workflow.

For a replacement controller, install Node Plane, sign in as the same Telegram administrator, import the snapshot and restore it. The current administrator's authentication is retained and ownership is transferred from their former administrator UUID. Restore SSH access to the nodes on the replacement controller first: the snapshot does not transfer private key files from the previous host. Review node reconnect results and obtain new workstation account credentials after restore. This is a manual recovery workflow; fully automated installation from a snapshot remains future work.

Telegram: administrator **Backups**. Open **Backups** to create a configuration backup, inspect the catalog or restore a selected backup. **Auto backups** controls scheduled backups, frequency and retention. Download sensitive backups to protected storage outside the controller.

A configuration backup is not a complete host image. It does not preserve temporary credentials or replace your server/database disaster-recovery plan. Restoration can change accounts, profile grants and settings; review its plan and resolve outstanding work first.

Restore reconnects the enabled nodes recorded in the backup, reuses reachable agents and installs missing or unreachable agents using the saved node connections. Each node has its own progress and result. A partial restore lists the nodes that still need attention; their recovery can be continued from their server cards.

## Alerts, diagnostics and audit

- **Alerts:** current/recent server alerts and notification preferences. Dismiss removes attention highlighting; it does not fix the server.
- **Controller diagnostics:** health checks and actions for reported problems.
- **Recovery history:** recovery outcomes and details.
- **Workstation audit:** administrator actions with account attribution and workstation key identity.

Open recovery from either interface:

- Workstation (NPW): **Diagnostic → Operations**.
- Telegram administrator menu: **System settings → Maintenance → Diagnostics & recovery**.

Select the affected operation, read its reason and choose an offered action: check its result, continue saved work or cancel safely. Available actions depend on the operation and its execution state; cancellation preserves changes already completed. Checking a result observes the saved operation without repeating installation. If rollback is confirmed, the update lock is released and a new update can be started. Missing or conflicting evidence keeps the operation blocked: do not delete journals or resend an uncertain mutation to clear its status.

If an agent setup stops at **Archiving previous installation journals** after removing a node and adding the same VPS under a new key, check that operation's result in Operations. The controller checks the agent's old identity over SSH and verifies that its old key was permanently removed from this controller. When confirmed, Continue/Resume becomes available for the same operation. Its old journals are archived before rebinding; an agent belonging to another active node remains protected.

SSH agent setup permits its TCP port in active UFW only from the controller's observed SSH source IP. It does not enable UFW or change its default policy. VPN installation, reinstallation and settings application also allow the selected protocol ports, including the effective automatic AWG port. Provider firewalls and other firewall managers must be configured separately.

## Reset and removal

Open **Reset & removal** in Node Plane Settings or the bot's cleanup settings. Review the plan and type the displayed confirmation phrase.

**Controller reset** clears controller configuration while retaining the installation and the initiating administrator's account/profile. If node cleanup is not selected, installed node configurations and existing VPN access remain on those hosts. Select node cleanup to revoke access and remove managed nodes as part of the operation.

**Full removal** stops and removes owned controller resources. The plan identifies affected nodes and data. External database hosting, unrelated containers and shared host packages are not automatically removed. The final goodbye is shown after the owned-resource verification succeeds.

After removal, Workstation disables controller actions. Use a new-installation action if you want to install again under the same local connection profile.

Monitoring uses a fixed cadence, independently of notification preferences. CPU is sampled every 5 seconds and evaluated as a rolling 1-minute average; resource and service checks run every 30 seconds. CPU warning starts at 85% and critical at 95%. Separate recovery thresholds and confirmation checks prevent brief fluctuations from repeatedly closing and reopening an alert. Missing, incomplete or stale measurements stay unknown.

**Reminders** controls repeat notifications for a continuing problem: **Off / 30m / 1h / 3h**, with **1h** as the default. Off keeps the initial alert, escalation to critical and confirmed recovery notifications; it disables only reminders. A recovery notification includes the current measurement. Close deletes only the notification message; alert Inbox and Dismiss behavior are unchanged.

Agent-unavailable alerts require repeated failed connectivity probes. A timeout or failure of the full Docker/runtime inspection is checked against a lightweight authenticated metrics request before declaring the agent unavailable. Successful fast metrics requests also confirm connectivity recovery, without waiting for the full service inspection. Controller logs identify the node and sanitized RPC status of failed checks.

Removing a VPN protocol automatically revokes its profile grants (including administrator grants) and temporary configurations on that node. Runtime cleanup waits for confirmed revocation; uncertain operations require recovery first. Other protocols and other nodes retain their access. Saved explicit grants for the removed protocol are cleared; persistent region/all-server rules remain in place and may grant access again if the protocol is later reinstalled. Administrator access returns automatically after services are installed.
