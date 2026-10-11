[Русская версия](maintenance.ru.md)

# Maintenance

Use the controller settings and updates screens in either interface. Read confirmations before operations that change access, restore data or remove resources.

## Updates

Workstation: **Node Plane Updates**. Telegram: administrator **Updates**.

The screen shows the branch, installed version, component state, latest known release and last check time. Run **Check** to query releases. Configure **Auto checks** to enable scheduled checks and choose their frequency; administrators receive a notification when a new release is found.

Review an update and start it. Progress shows component steps. **Components** and **Update result** provide node/component outcomes. Dismiss a result to remove its shortcut from Updates. Changing branch or rolling back requires a separate confirmation because it can break installation compatibility.

A controller update can restart the bot. Reopen Updates if progress is interrupted. Do not start another update while the existing outcome is uncertain.

For a direct server check:

```sh
cd /opt/node-plane/current
./scripts/healthcheck.sh --mode simple
```

## Backups

Open **Backups** to create a configuration backup, inspect the catalog or restore a selected backup. **Auto backups** controls scheduled backups, frequency and retention. Download sensitive backups to protected storage outside the controller.

A configuration backup is not a complete host image. It does not preserve temporary credentials or replace your server/database disaster-recovery plan. Restoration can change accounts, profile grants and settings; review its plan and resolve outstanding work first.

## Alerts, diagnostics and audit

- **Alerts:** current/recent server alerts and notification preferences. Dismiss removes attention highlighting; it does not fix the server.
- **Controller diagnostics:** health checks and actions for reported problems.
- **Recovery history:** recovery outcomes and details.
- **Workstation audit:** administrator actions with account attribution and workstation key identity.

If an operation is blocked, use its diagnostics and explicit recovery choices. A timeout can leave an unknown result. Do not delete journals or force a new mutation to clear the label.

## Reset and removal

Open **Reset & removal** in Node Plane Settings or the bot's cleanup settings. Review the plan and type the displayed confirmation phrase.

**Controller reset** clears controller configuration while retaining the installation and the initiating administrator's account/profile. If node cleanup is not selected, installed node configurations and existing VPN access remain on those hosts. Select node cleanup to revoke access and remove managed nodes as part of the operation.

**Full removal** stops and removes owned controller resources. The plan identifies affected nodes and data. External database hosting, unrelated containers and shared host packages are not automatically removed. The final goodbye is shown after the owned-resource verification succeeds.

After removal, Workstation disables controller actions. Use a new-installation action if you want to install again under the same local connection profile.
