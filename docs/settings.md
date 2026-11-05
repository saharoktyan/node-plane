[Русская версия](settings.ru.md)

# Installation settings

Controller settings are shared by Telegram and Workstation. Workstation's own appearance and connection preferences are under its separate **Settings** section.

## Installation defaults

Open **Node Plane Settings → Basic → Installation defaults** in Workstation or installation defaults in the bot's administrator settings.

Choose protocols, Xray transports and port modes. Expand **Advanced options** for SNI, fingerprint, XHTTP path and AWG parameters. In Telegram, clickable parameter labels open their editors. Auto AWG mode displays the preset's port preference.

Edit values, review the draft and save. Reset draft discards unsaved changes. Defaults supply values for future deployments; edit an existing server in its own settings.

## Policies

Traffic collection is controlled by an administrator. Enable it to collect supported profile traffic statistics. Server cards also expose monthly aggregate traffic when available. Disabling profile collection affects the user-facing statistics; counters are operational observations, not a quota enforcement system.

**Requests settings** controls access requests and automatic approval rules. Check expiry and access grants when configuring approval; approving an account does not explain every protocol grant by itself.

## Bot name and SSH key

Set the displayed bot name in installation settings → Telegram; like the bot's other settings, it is hidden while the bot is off. The controller public SSH key is needed only for the bot's **Install over SSH**: add it to the server's `authorized_keys` yourself. Copy it or use the bot's separate public-key message action. Keep private keys private.

## Relay

The relay is the web cabinet's public address. It runs as a service on a node with an installed agent; the controller connects out to it and is never exposed. Set it up from the bot's **System settings → Relay** or Workstation's **Settings → Basic → Relay**: choose the node, the cabinet's domain and how the relay gets TLS (Let's Encrypt on port 443, certificate files on the node, or a reverse proxy in front of it). The same screen opens from a node: its settings add the relay to that node, and a node with the relay has **Relay settings** on its card. Back returns the way you came. One relay per installation; owners and administrators manage it.

## Announcements

Choose recipients, write the message and preview it before sending. The preview shows how many recipients get it in Telegram and how many only in the Inbox (people without Telegram, or every recipient while the bot is off). Inspect delivery results. Recipients can close a received message and reopen it in Notifications; dismiss removes it from their list.

For backups, alerts, audits and cleanup, see [Maintenance](maintenance.md).
