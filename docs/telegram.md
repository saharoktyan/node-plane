[Русская версия](telegram.ru.md)

# Telegram bot

The bot provides user configuration delivery and administrator controls for the same Node Plane installation managed by Workstation.

## First start

Open your installed bot and send `/start`. The Telegram IDs supplied during installation receive administrator access. Other users can request access according to the installation's request policy.

The navigation line at the top shows your location. Click a parent item to return to that screen. Long paths are shortened to fit the interface. **Back** returns one level; a confirmation protects unsaved server/profile drafts.

## User actions

- **Get config:** choose a server, protocol and Xray transport when choices are available. For AWG, choose a device when there is more than one. Download the file, use the URI or scan the QR with a compatible VPN client.
- **Profile:** view access status, expiry, allowed protocols and available device actions.
- **Settings:** change available personal preferences.
- **Notifications:** read administrator announcements. This section is hidden when empty. **Close** closes the message; **Dismiss** also removes it from your notification list.

Use the separate-message action when an iOS client cannot copy a link or public key from the formatted screen. Treat delivered configurations as private credentials.

## Administrator actions

The administrator menu provides servers, profiles, access requests, updates, settings and announcements. Start with these guides:

1. [Create a node and run setup](nodes.md).
2. [Approve requests and grant access](access.md).
3. [Configure installation defaults and policies](settings.md).
4. [Update, back up and inspect alerts](maintenance.md).

Temporary configurations are available in administrator settings. They have their own list and revocation actions; see [Temporary configurations](temporary-configs.md).

## Operation progress

Active setup, update, deletion and cleanup screens refresh their progress. Leaving the screen does not cancel an accepted operation. Reopen its status to check the outcome. If a job is blocked, inspect its reason and diagnostics before starting another mutation.

Update notifications and alerts have their own close/dismiss actions. Closing a notification preserves the menu you were using. Dismissing alert attention does not repair the underlying condition.
