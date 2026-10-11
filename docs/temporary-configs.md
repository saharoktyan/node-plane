[Русская версия](temporary-configs.ru.md)

# Temporary configurations

Administrators can create independent VPN credentials for **12 hours**, **1 day** or **3 days**. They do not require granting a permanent user profile access.

## Create

In Workstation, open **Temporary configs**, or open it from a Nodes card to use that server. In Telegram, open temporary configurations in administrator settings.

1. Choose a server.
2. Choose a protocol if more than one is available.
3. For Xray, choose a transport if more than one is available.
4. Choose 12h, 1d or 3d.
5. Review and confirm.
6. Wait for active status, then open the configuration.

Workstation can copy the URI and save files. Telegram offers the configuration screen with file/link and QR actions, including separate-message delivery for iOS.

## Manage

The list shows server, protocol, expiry and operation state. Open an active entry to retrieve it again or revoke it early. Expired/revoked entries leave the active list. Pending and blocked work remains visible until resolved.

Temporary credentials cannot be extended or converted to permanent credentials. Create another temporary configuration if another period is needed.

## Expiry and revocation

**VLESS:** new connections are blocked after expiry or revocation. An existing connection may continue until it disconnects. This also applies to ordinary profile expiry and manual VLESS revocation.

**AWG:** expiry/revocation removes the independent peer. The node needs working protocol administration to apply that removal.

The node keeps expiry work locally and runs it independently of controller/agent availability. Host shutdown, failed protocol administration or a busy operation can delay confirmed removal. A blocked status requires inspection; it is not confirmation that a tunnel has stopped.

Temporary credentials are excluded from configuration backups. Resolve outstanding temporary access before a backup restore that requires it. Treat downloaded files and copied URIs as secrets.
