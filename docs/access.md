[Русская версия](access.ru.md)

# Profiles and access

An account identifies a person and their role. A VPN profile holds that person's VPN access, expiry, devices and grants. Both interfaces present these together under profiles. A Workstation installation profile is a local connection entry and is unrelated to VPN access.

## Requests

Open **Access → Requests** in Workstation or the administrator's requests screen in Telegram. Review the Telegram ID, username when available and request details. Approve or reject the request. Approval and protocol grants are separate choices: check which servers the profile can use.

Request policy controls whether users can request access and which requests are handled automatically; see [Settings](settings.md).

## Grant access

Open or create a profile, then edit its access:

1. Set the profile name and access duration.
2. Select the servers and protocols to grant.
3. Configure future-server rules when this profile should automatically receive access to matching new servers.
4. Review and save the draft.
5. Check the operation result before issuing configurations.

The expiry display shows the resulting deadline. Current grants and future-server rules are separate: check both when changing a region or removing access.

## Devices and configurations

AWG devices have distinct identities and peer configurations. Create a separate device for each simultaneous client. Two clients sharing one AWG identity can interfere with each other's connection.

The user chooses an allowed server and protocol in Telegram. With one AWG device, the bot opens it directly; with several devices, it offers a choice. Remove a device to revoke its peer. Device removal does not delete the account.

## Roles, blocking and freezing

An administrator role grants management permissions. Change a role only for someone who should manage the installation.

**Block account** disables bot/API access. Installed VPN access is retained; use **Freeze profile** to suspend it while keeping profile settings. After freezing or unfreezing, inspect the runtime operation status.

Expiry and revocation remove runtime authorization. New VLESS connections are blocked, but established connections may continue until they disconnect. See [Temporary configurations](temporary-configs.md) for the same limitation on short-lived access.

## Delete a profile

Review the affected access and confirm deletion. Progress refreshes while the operation runs. A blocked revocation requires recovery; closing the screen does not complete or cancel it.

Deletion of a saved Workstation installation profile is a local preference action. It does not delete a user, VPN profile or controller.
