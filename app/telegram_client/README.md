# New Telegram client (early slice)

This aiogram 3 client calls the standalone backend over its loopback HTTP API.
It does not import backend business repositories, the driver, or the legacy
PTB handlers. Every screen uses structured Rich Message blocks. Member screens
embed callback buttons beside their associated server or setting; the callback
tokens remain short and ephemeral. The member flow covers
registration/access request, profile selection, node selection, and current
Xray or AWG artifact issuance. An administrator can decide pending access
requests, create a VPN profile for an account, freeze it, and add or remove
node/protocol grants. These mutations use backend revisions. The admin can
also register a disabled node with default AWG/Xray settings, queue local or
SSH agent installation, inspect its state, probe it, edit desired settings and
protocols, and apply pending settings.
Node/profile creation and SSH agent setup retain their idempotency keys across
a lost backend response. SSH setup uses port 22 and the controller's configured
SSH key; key paths and private keys are never accepted from Telegram. After
the agent is installed, Apply settings prepares the runtime, installs Docker
if necessary, deploys protocols, and enables the node only after verification.
The admin maintenance screen binds an independently checked host, drains grants,
advances runtime/agent cleanup one phase at a time, and removes the registry
record only after final host verification. A separate, explicitly confirmed
registry-only action is available for an unreachable VPS; it does not claim
the remote files were removed. The update screen compares the node runtime
version with this release and can stage the current runtime. Agent binary
reinstallation remains on the node card. Config issuance embeds files and,
when it fits, a collapsed QR image in the control message. Admins listed in `ADMIN_IDS` receive a
best-effort Rich Message when a user requests access; a decision notifies the
requester. These notifications are not durable across bot crashes.

In simple mode, `scripts/install.sh` initializes the backend schema, bootstraps
each Telegram administrator listed in `ADMIN_IDS`, creates an adapter credential
on first install, and starts the backend, worker timer, and aiogram client.
It disables the old `node-plane.service` when activating
`node-plane-telegram.service`; startup failure restores an active old service.
The adapter credential path is stored in the shared `.env` and preserved across
reinstalls. `NODE_PLANE_BACKEND_URL` defaults to `http://127.0.0.1:8080`.
For a manual installation, the individual unit installer remains available:
`scripts/install_telegram_client_systemd.sh BASE_DIR SHARED_DIR --activate`.
Test the new flow with a disposable node before using it for production access.
The simple-mode updater and health check recognize the active new service;
updating it does not run the legacy registry-based agent rollout.

The control message ID and callback tokens currently live in process memory.
After a restart, old buttons expire safely and `/start` recreates the screen.
Config screens contain collapsed QR, monospace import-link, and help blocks.
AWG includes both downloadable `.vpn` and `.conf` files. Import labels use
`Server name Protocol [Transport] · Profile name`; AWG passes this description
to the node's converter and VLESS stores it in the URI fragment.
Server sections provide protocol buttons directly, without another selection
screen. Settings place each action below its current value. Navigation sits at
the bottom as link-style buttons. When Rich Message delivery is rejected by
Telegram, the screen falls back to plain text with equivalent inline navigation;
files and QR are then sent separately. All member text is localized in RU/EN.
A real Telegram client acceptance pass on Android, iOS, and Desktop is still
required for rendering and document/QR behavior.
