[Русская версия](settings.ru.md)

# Installation settings

Controller settings are shared by Telegram and Workstation. Workstation's own appearance and connection preferences are under its separate **Settings** section.

## Installation defaults

Open **Node Plane Settings → Installation → Installation defaults** in Workstation or installation defaults in the bot's administrator settings.

Choose protocols, Xray transports and port modes. Expand **Advanced options** for SNI, fingerprint, XHTTP path and AWG parameters. In Telegram, clickable parameter labels open their editors. Auto AWG mode displays the preset's port preference.

Edit values, review the draft and save. Reset draft discards unsaved changes. Defaults supply values for future deployments; edit an existing server in its own settings.

## Policies

Traffic collection is controlled by an administrator. Enable it to collect supported profile traffic statistics. Server cards also expose monthly aggregate traffic when available. Disabling profile collection affects the user-facing statistics; counters are operational observations, not a quota enforcement system.

**Requests settings** controls access requests and automatic approval rules. Check expiry and access grants when configuring approval; approving an account does not explain every protocol grant by itself.

## Bot name and SSH key

Set the displayed bot name in installation settings. The controller public SSH key is used for remote node access. Copy it or use the bot's separate public-key message action. Keep private keys private.

## Announcements

Choose recipients, write the message and preview it before sending. Inspect delivery results. Recipients can close a received message and reopen it in Notifications; dismiss removes it from their list.

For backups, alerts, audits and cleanup, see [Maintenance](maintenance.md).
