# New Telegram client (early slice)

This aiogram 3 client calls the standalone backend over its loopback HTTP API.
It does not import backend business repositories, the driver, or the legacy
PTB handlers. Every screen uses structured Rich Message blocks; inline buttons
only carry short, ephemeral navigation tokens. The member flow covers
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
reinstallation remains on the node card. Config issuance includes a file and,
when it fits, an on-demand QR image. Admins listed in `ADMIN_IDS` receive a
best-effort Rich Message when a user requests access; a decision notifies the
requester. These notifications are not durable across bot crashes.

For a manual test, initialize the backend with the systemd installer, then
bootstrap an admin and create an adapter credential as documented in
[`app/backend/README.md`](../backend/README.md). Set
`NODE_PLANE_BACKEND_ADAPTER_TOKEN_FILE` to that credential file and run
`PYTHONPATH=app .venv/bin/python -m telegram_client.main` with `BOT_TOKEN` and
the shared environment loaded. `NODE_PLANE_BACKEND_URL` defaults to
`http://127.0.0.1:8080`. The legacy bot and this client must not poll the same
Telegram token at the same time. Run
`scripts/install_telegram_client_systemd.sh BASE_DIR SHARED_DIR` to install an
inactive systemd unit. Pass
`--activate` only when ready to stop and disable the legacy
`node-plane.service` and start the new `node-plane-telegram.service`; startup
failure restores the previous service and its enabled state. The script requires `BOT_TOKEN` and
`NODE_PLANE_BACKEND_ADAPTER_TOKEN_FILE` in the shared `.env`. Test the new flow
with a disposable node before switching the production Telegram token.
The simple-mode updater and health check recognize the active new service;
updating it does not run the legacy registry-based agent rollout.

The control message ID and callback tokens currently live in process memory.
After a restart, old buttons expire safely and `/start` recreates the screen.
Configuration files are separate Telegram documents because this flow needs
downloadable `.vpn`/`.conf` artifacts. When Rich Message delivery is rejected
by Telegram, the screen falls back to plain text with the same navigation.
A real Telegram client acceptance pass on Android, iOS, and Desktop is still
required for rendering and document/QR behavior.
