[Русская версия](access.ru.md)

# Profiles and access

An account identifies a person and their role. A VPN profile holds that person's VPN access, expiry, devices and grants. Every account has exactly one VPN profile, including accounts without server access. Both interfaces present them together under profiles. A Workstation installation profile is a local connection entry and is unrelated to VPN access.

## Requests

Sending `/start` and language selection record visitor metadata only; an access request exists without allocating an account. Pending is a request and presentation status, not a new VPN profile. Approval or explicit administrator provisioning creates the account and default VPN profile atomically. Rejecting a request or background menu refresh does not allocate an account.

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

In Telegram, **Server access** lists one button per server, with selected protocols beside its name. Open a server to change its protocols; Back returns to the same list page. The text actions **All / None** apply to all current servers, or only the region beside them, across all pages. **Persistent rules** opens future-server rules. Changes remain a draft until saved.

## Devices and configurations

AWG devices have distinct identities and peer configurations. Create a separate device for each simultaneous client. Two clients sharing one AWG identity can interfere with each other's connection.

The user chooses an allowed server and protocol in Telegram. With one AWG device, the bot opens it directly; with several devices, it offers a choice. Remove a device to revoke its peer. Device removal does not delete the account.

## Roles, blocking and freezing

An administrator role grants management permissions. Change a role only for someone who should manage the installation.

**Block account** disables bot/API access. Installed VPN access is retained; use **Freeze profile** to suspend it while keeping profile settings. After freezing or unfreezing, inspect the runtime operation status.

Expiry and revocation remove runtime authorization. New VLESS connections are blocked, but established connections may continue until they disconnect. See [Temporary configurations](temporary-configs.md) for the same limitation on short-lived access.

## Delete an account

**Delete account** removes the account, its VPN profile and devices. After deletion, submitting a new request alone does not recreate an account; a subsequent approval or explicit administrator provisioning creates a fresh account and profile with new IDs. **Freeze** and **Block** preserve the existing account and profile. Review the affected access and confirm deletion. Progress refreshes while the operation runs. A blocked revocation requires recovery; closing the screen does not complete or cancel it.

Deletion of a saved Workstation installation profile is a local preference action. It does not delete a user, VPN profile or controller.

Administrator VPN profiles have permanent access to every current and future server once its protocols are installed. Their access rules cannot be edited, and administrators cannot be frozen, blocked or deleted. Demote an administrator before applying these restrictions; demotion restores the previous member access policy. Profile deletion progress refreshes automatically while its panel is open, until all remote revocations are confirmed.
