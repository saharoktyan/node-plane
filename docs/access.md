[Русская версия](access.ru.md)

# Profiles and access

An account identifies a person and their role. A VPN profile holds that person's VPN access, expiry, devices and grants. Every account has exactly one VPN profile, including accounts without server access. Both interfaces present them together under profiles. The account keeps its own display name and language: they start from the person's Telegram name and language and stay with the account whichever way the person signs in. A Workstation installation profile is a local connection entry and is unrelated to VPN access.

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

The user chooses an allowed server and protocol in Telegram. With one device, the bot opens it directly; with several devices, it offers a choice. Remove a device to revoke its AWG peer and VLESS client. Device removal does not delete the account. **New VLESS key** gives the device a new VLESS UUID: its old VLESS links stop working and AWG configurations do not change. **New AWG key** gives the device a new AWG peer with keys generated on the node; the old peer is removed from every node, after which its configurations stop working, and VLESS links do not change. Get the configuration again after either. Administrators can also reissue keys in Workstation (Access → profile → Devices → device), which shows the device count against the profile's device limit.

The `devices.max` VPN policy limits the number of active devices per profile. Set it for the installation and override it per profile. Reaching the limit only blocks new devices; existing devices keep working.

Profiles created before VLESS devices keep their link: on upgrade the profile's VLESS client moves to its default device with the same UUID, so no client needs to be reconfigured.

## Simultaneous networks

The **Simultaneous networks** setting limits from how many networks one VLESS device connects at the same time. It discourages sharing one link: when a device connects from a new network over the limit, the node closes the connections of the oldest network of that device and blocks it for a cooldown. A network is one IPv4 address or one IPv6 /64, counted while it opened a connection within the window.

- `connections.mode`: `off` (default), `observe` (only report what would be disconnected) or `enforce`. Set it for the installation and override it per profile.
- `connections.max_sources`: networks per device, 1 by default. Set it for the installation and per profile.
Open it in Telegram → Settings → VPN → Simultaneous networks, in a profile's Edit screen, or in Workstation → Node Plane Settings → Policies.

- `connections.window_seconds` (120) and `connections.cooldown_seconds` (30) apply to the whole installation. A network blocked by the limit counts again only when it opens a new connection after its cooldown.

Each node receives the full set of its limits from the controller within a minute and enforces it on its own, including while the controller is unreachable. The settings screen lists nodes that do not have the current limits yet. A node whose agent is too old to enforce limits is named there; update the agent. Limits apply to VLESS only; AWG is not affected. Other devices and users are never blocked by another device's limit.

The device card in Telegram shows how many networks the device uses now out of its limit.

When a device is disconnected, its owner receives a service notification, at most once per 30 minutes per device. When one device reaches 5 disconnections (or, in observe mode, 5 would-be disconnections) within 24 hours, administrators receive a **VLESS link is probably shared** alert; the next alert needs 5 new events. The controller stores only these events and per-device network counts, never addresses; events are kept for 7 days.

Xray needs its online statistics and routing API for this. Applying node settings turns them on.

## Speed limits

**Speed limit** caps the upload and download speed of each device, on every server, for VLESS and AWG alike. Set it in Telegram → Settings → VPN → Speed limit for the installation, override it in a profile's Edit screen, or use Workstation → Node Plane Settings → Policies.

- `speed.up_kbit` and `speed.down_kbit`: kbit/s per device, no limit by default. A profile can choose **No limit** to be exempt from the installation limit. Each direction is set on its own.

Each node receives its limits within a minute and keeps enforcing them without the controller; the settings screen names nodes that do not have the current limits or cannot apply them. Nodes shape traffic with `tc` and `nft` and enter the AWG container with `nsenter`, so the host needs `iproute2`, `nftables` and `util-linux` and the `ifb` kernel module. Node preparation and agent installation or update install the missing packages on Debian and Ubuntu. A node without them reports that it cannot limit speed; other traffic is unaffected. Limits apply per device, not per profile: two devices of one profile each get the full limit. After **New AWG key**, the old peer keeps the device's limits until every node removed it.

VLESS traffic is shaped on the interfaces of the default routes. When it leaves through another interface, name it in `/etc/node-plane/node.env` on the node, for example `SHAPING_UPLINKS=eth1` (several separated by commas); applying node settings keeps this line.

## Device and node values

Simultaneous networks and speed limits can also be set for one device and for one node. A device value overrides its profile: Telegram → device card → **Device limits**, or Workstation → Access → profile → Devices → device → **Limits**. A node value overrides the installation value on that node: Telegram → server → Settings → **Limits on this node**, or Workstation → Nodes → node → **Limits**, which also holds the node's BitTorrent setting. **As profile** or **As installation** removes the own value. Workstation → Access → profile → **Limits** shows all values of a profile, including its traffic quota.

## Traffic quotas

A traffic quota limits uplink and downlink together across all servers of a profile within a period. Set it in Telegram → Settings → VPN → Traffic quota for the installation, override it in a profile's Edit screen, or use Workstation → Node Plane Settings → Policies. Traffic collection must be on.

- **Limit per period** (`quota.bytes`): no limit by default. A profile can choose **No limit** to be exempt from the installation quota.
- **Reset** (`quota.reset`): on the 1st of each month; monthly on the start day (the last day of shorter months); every N days (`quota.interval_days`); or never, for a one-off allowance. Periods follow the installation time zone (`quota.timezone`, UTC by default).
- **When used up** (`quota.on_exhausted`): suspend access until the next reset (default), only notify, or limit the speed of every device of the profile to `quota.throttle_kbit` (1 Mbit/s by default) until the next reset or a raised limit. A lower speed limit of a device stays.

The start day is when the profile first got a quota; an administrator can move it to another date in the profile's quota screen; the date is midnight in the quota time zone. The installation time zone is chosen from common zones or typed as an IANA name such as `Europe/Riga`. Changes to the reset take effect from the next reset, and the current usage stays; a new limit applies at once, so raising it restores suspended access. **Reset now** in the profile's quota screen starts a new period immediately and restores suspended access.

A quota suspension is separate from freezing: a new period lifts only the quota suspension, never an administrator's freeze. Owners receive service notifications at 80% and when the quota is used up, and after a reset that restored access. Usage is counted from traffic collection, so a profile can exceed its limit by up to one collection interval.

## Tariffs

A tariff is a named set of limits with an access term and nodes. Its name and an optional description are written per language (see [Texts in several languages](settings.md#texts-in-several-languages)); people see them in their language. Create and edit tariffs in Telegram → Settings → VPN → **Tariffs** or Workstation → Node Plane Settings → Policies → **Tariffs**; assign one in a profile's Edit screen in Telegram (**Change tariff**) or with **Tariff** on the profile card in Workstation.

- **Limits**: devices per profile, simultaneous networks, speed and traffic quota, with the same values as the installation settings. They stay linked: a tariff's values apply to every profile on it and follow its changes. A profile's own value still wins, and **As tariff** shows what removing it falls back to. A limit the tariff does not set comes from the installation.
- **Access term** in days, or no term. Assigning can keep the profile's term, start a new one from now, or extend it: **Extend** adds the term after the remaining time, or from now when access has already expired. Assigning the same tariff again with **Extend** is a renewal.
- **Nodes**: all nodes, chosen regions (new nodes in them are added automatically), or keep the profile's nodes. A tariff's nodes are copied only when you choose **Replace nodes** while assigning it; configs on nodes the profile loses stop working.

Editing a tariff never changes a profile's term or nodes. Removing a tariff from a profile stops its limits and keeps the term and nodes. A tariff assigned to profiles cannot be deleted. Administrators' profiles may have a tariff for its limits but keep their unlimited term and access to every node.

### Renewal requests

A member whose access has a term sees **Extend access** on their profile card in Telegram. The request goes to administrators as a notice with the decision buttons and waits under **Requests → Renewal requests**; in Workstation a profile with a pending request shows **Renewal** on its card. An administrator extends by the profile's tariff term or, without one, by 7 to 365 days, or declines. The term is added after the remaining time, or from now when access has expired. One request per profile waits at a time, and the owner gets a service notification with the outcome. Requests are not part of backups.

## Roles, blocking and freezing

An administrator role grants management permissions. Change a role only for someone who should manage the installation.

Staff hold roles; every other account is a member, who manages only their own profiles. The roles are fixed presets:

| Role | May |
|---|---|
| Owner | everything, including changing other owners; an installation always keeps one owner |
| Administrator | everything except changing owners |
| Operator | servers and their services (Xray, AmneziaWG): setup, settings, diagnostics, maintenance |
| Support | access requests, profiles, and issuing configurations for people |

The first administrators of an installation are owners. Promoting a member makes an administrator; only an owner demotes an owner.

Each type of service on servers has its own permission (`services.xray.manage`, `services.awg.manage`); owners, administrators and operators hold them. Changing a server's settings, or applying them, needs the permission of every engine the change touches; a setting shared by the engines, such as the public address, touches all of them.

Notifications follow the roles: access requests go to those who handle them (owners, administrators, support), node alerts to those who manage servers (owners, administrators, operators), shared-link alerts to those who manage profiles, and update notices to owners and administrators. The bot's administration menu shows only the sections a role allows.

In Workstation, **Role** on an account card picks the role. Any administrator makes a member an administrator or demotes staff other than owners; only an owner assigns the other presets. Through the API, `PATCH /api/v1/accounts/{id}` takes `roles` (the exact list, empty for a member) with the `roles.manage` permission, or `role` (`admin`/`member`) as before.

**Block account** disables bot/API access. Installed VPN access is retained; use **Freeze profile** to suspend it while keeping profile settings. After freezing or unfreezing, inspect the runtime operation status.

Expiry and revocation remove runtime authorization and close established VLESS connections. Nodes whose Xray container still runs the upstream image, deployed before this release, only block new connections until the container is redeployed. See [Temporary configurations](temporary-configs.md) for short-lived access.

## Invites

An invite is a one-time link that registers someone without a request. In the bot, **Accounts → Invite** asks what the new account gets: nothing yet, VPN with the default access, or VPN with a tariff (its access and term). The link is `t.me/<bot>?start=inv_…`; when the person opens it, the bot creates their account with their Telegram and offers to set a login. With a relay, the same invite also has a web cabinet link, where the person picks a login and a password or passkey. In Workstation, invites have their own **Invites** tab: **New invite**, **Bot username**, and **Revoke** on an active one. A link works once and for 7 days, and is shown only when it is made.

The bot's link names the bot by its username, which the controller keeps. While Telegram is on, an invite for a new account cannot be made without it: Workstation asks for it at the first **New invite** (later under **Bot username**), and the bot fills in its own. A link opens only while the bot keeps that username: after a change, links made before it stop working, so make new ones.

The template is applied with the rights its author has when the link is used: if the author is blocked or loses the right to manage profiles, the invite stops working. Invites give member rights only; assign staff roles afterwards.

**Sign-in link** on an account card in Workstation makes an invite for that account: with it, the person sets a login and password in the web cabinet, for the first time or after losing the password (a new password ends the other sessions). Such a link signs in as the account, so it is never made through Telegram. Restoring a backup voids all invites.

## Open registration

Registration without an invite is off by default. Turn it on in the bot's **System settings → Registration** or under **Registration** on Workstation's **Invites** tab. Then anyone who reaches the web cabinet can make an account with a login and a password or passkey. Such an account gets no access and no automatic request: the person asks for a service under **Access to services**.

To keep a leaked domain from flooding the installation, each registration solves a short proof-of-work puzzle in the browser (no third-party CAPTCHA), and one address makes at most 5 accounts a day. Accounts without access and without requests stay out of the main list: they are under **Accounts → Without access** in the bot and **Without access** on Workstation's **Profiles** tab. Optionally, registered accounts left like that are removed after 7, 30 or 90 days without a sign-in; this is off by default too.

## Login

An account can have a login: the name it will sign in to the web cabinet with. A login is 3–32 Latin letters, digits, `_`, `.` or `-`, starts with a letter or digit, and is unique regardless of case; names such as `admin`, `root` and `support` are reserved. Everything else refers to the account itself, so changing a login breaks nothing.

A member sets their login in the bot's **Settings → Login**; the bot offers their Telegram username when it fits and is free. A member can change it once in 30 days, and the old login stays reserved for them for 30 days. Administrators change any account's login at any time: on the account card in the bot, or with **Login** on the account card in Workstation's **Accounts**. Deleting an account frees its login after 30 days.

## Password, passkeys and sessions

With a login, an account can have a password for the web cabinet. A password is never entered in Telegram: an administrator sets their own with **Password** on their account card in Workstation's **Accounts**, and members will set theirs in the web cabinet. A password is 10–256 characters and differs from the login; changing it needs the current one and signs the account out everywhere else.

A passkey signs in without a login or password: Face ID or Touch ID with iCloud Keychain, Google Password Manager, Windows Hello or a hardware key. Passkeys are added in the web cabinet and work only on the cabinet's domain, the relay's, so they need a relay; an account can have up to 20. Like a password, a passkey is never added through Telegram.

Each device signed in with a password or passkey is a session. A session lasts 30 days from its last use. Repeated wrong passwords pause signing in to that login, or from that address, for 15 minutes. In the bot, **Settings → Sign-in and sessions** shows whether a password is set and how many passkeys there are, and lists the sessions, each with its own **End** and **Sign out everywhere** for all of them, and the passkeys, each with **Remove passkey**. Administrators see on the account card, in the bot and in Workstation, whether an account has a password and how many sessions and passkeys it has; **End sessions** signs it out on every device, and **Remove passkeys** (after a confirmation) removes its passkeys, such as after a lost device. Restoring a backup ends every session; passwords and passkeys are kept.

## Telegram in the web cabinet

An account with Telegram signs in to the web cabinet with it: with the Telegram Login button, or right away when the cabinet is opened as a Mini App from the bot. The controller checks Telegram's signature with the bot's token, accepts only fresh data and each set once, so this needs the bot on.

A Telegram belongs to one account. To move it to the account you use in the cabinet, sign in there and link Telegram, or take a code in the bot's **Settings → Sign-in and sessions → Move Telegram to a cabinet account** and enter it in the cabinet within 10 minutes. Before you confirm, the cabinet shows what happens:

- the Telegram belongs to no account: it is linked;
- it belongs to an account without access, requests or staff roles: it moves, and that empty account is deleted;
- it belongs to an account with access: the move is refused, since that account would keep its access with no way to sign in. Use that account instead: sign in to the cabinet with Telegram.

Accounts and their access are never merged by a move.

## Merge accounts

A person with two accounts can make them one, by request. In the account to be merged in, **Settings → Sign-in and sessions → Merge accounts** gives a code; the main account enters it, in the web cabinet or the bot, within 10 minutes. Holding both proves they are one person's. Staff accounts are not merged in, and either side can cancel while the request waits.

An administrator decides under **Accounts → Merges** in the bot or the **Merges** tab in Workstation. Each account is shown on its own, with what the merge does: the main account keeps itself and gets the other's VPN profile, its Telegram, and its password and login when it has none; the other's passkeys are removed, since each names its account; the other account is deleted. When both have a VPN profile, the administrator picks which stays and the other is deleted and revoked on its nodes; an administrator's own VPN always stays. The request keeps what was shown and decided.

## Delete an account

**Delete account** removes the account, its VPN profile and devices. After deletion, submitting a new request alone does not recreate an account; a subsequent approval or explicit administrator provisioning creates a fresh account and profile with new IDs. **Freeze** and **Block** preserve the existing account and profile. Review the affected access and confirm deletion. Progress refreshes while the operation runs. A blocked revocation requires recovery; closing the screen does not complete or cancel it.

Deletion of a saved Workstation installation profile is a local preference action. It does not delete a user, VPN profile or controller.

Administrator VPN profiles have permanent access to every current and future server once its protocols are installed. Their access rules cannot be edited, and administrators cannot be frozen, blocked or deleted. Demote an administrator before applying these restrictions; demotion restores the previous member access policy. Profile deletion progress refreshes automatically while its panel is open, until all remote revocations are confirmed.
