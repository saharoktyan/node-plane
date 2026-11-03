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

A device is shared by both protocols: it has its own AWG peer and its own VLESS client (a separate UUID). Create a separate device for each simultaneous client. Two clients sharing one identity can interfere with each other's connection, and per-device identities give per-device traffic.

The user chooses an allowed server and protocol in Telegram. With one device, the bot opens it directly; with several devices, it offers a choice. Remove a device to revoke its AWG peer and VLESS client. Device removal does not delete the account. **Reissue key** gives the device a new VLESS UUID: its old VLESS links stop working and AWG configurations do not change.

The `devices.max` VPN policy limits the number of active devices per profile. Set it for the installation and override it per profile. Reaching the limit only blocks new devices; existing devices keep working.

Profiles created before VLESS devices keep their link: on upgrade the profile's VLESS client moves to its default device with the same UUID, so no client needs to be reconfigured.

## Simultaneous networks

The **Simultaneous networks** setting limits from how many networks one VLESS device connects at the same time. It discourages sharing one link: when a device connects from a new network over the limit, the node disconnects the oldest network of that device and blocks it for a cooldown. A network is one IPv4 address or one IPv6 /64, counted while it opened a connection within the window.

- `connections.mode`: `off` (default), `observe` (only report what would be disconnected) or `enforce`. Set it for the installation and override it per profile.
- `connections.max_sources`: networks per device, 1 by default. Set it for the installation and per profile.
Open it in Telegram → Settings → VPN → Simultaneous networks, in a profile's Edit screen, or in Workstation → Node Plane Settings → Policies.

- `connections.window_seconds` (120) and `connections.cooldown_seconds` (300) apply to the whole installation.

Each node receives the full set of its limits from the controller within a minute and enforces it on its own, including while the controller is unreachable. The settings screen lists nodes that do not have the current limits yet. A node whose agent is too old to enforce limits is named there; update the agent. Limits apply to VLESS only; AWG is not affected. Other devices and users are never blocked by another device's limit.

When a device is disconnected, its owner receives a service notification, at most once per 30 minutes per device. When one device reaches 5 disconnections (or, in observe mode, 5 would-be disconnections) within 24 hours, administrators receive a **VLESS link is probably shared** alert; the next alert needs 5 new events. The controller stores only these events and per-device network counts, never addresses; events are kept for 7 days.

Xray needs its online statistics and routing API for this. Applying node settings turns them on.

## Roles, blocking and freezing

An administrator role grants management permissions. Change a role only for someone who should manage the installation.

**Block account** disables bot/API access. Installed VPN access is retained; use **Freeze profile** to suspend it while keeping profile settings. After freezing or unfreezing, inspect the runtime operation status.

Expiry and revocation remove runtime authorization. New VLESS connections are blocked, but established connections may continue until they disconnect. See [Temporary configurations](temporary-configs.md) for the same limitation on short-lived access.

## Delete an account

**Delete account** removes the account, its VPN profile and devices. After deletion, submitting a new request alone does not recreate an account; a subsequent approval or explicit administrator provisioning creates a fresh account and profile with new IDs. **Freeze** and **Block** preserve the existing account and profile. Review the affected access and confirm deletion. Progress refreshes while the operation runs. A blocked revocation requires recovery; closing the screen does not complete or cancel it.

Deletion of a saved Workstation installation profile is a local preference action. It does not delete a user, VPN profile or controller.

Administrator VPN profiles have permanent access to every current and future server once its protocols are installed. Their access rules cannot be edited, and administrators cannot be frozen, blocked or deleted. Demote an administrator before applying these restrictions; demotion restores the previous member access policy. Profile deletion progress refreshes automatically while its panel is open, until all remote revocations are confirmed.
