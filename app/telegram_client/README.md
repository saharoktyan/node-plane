# Telegram client

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
best-effort Rich Message when a user requests access. A decision updates the
requester's existing control message with the result and a To menu button,
which opens the menu in the same message. If there is no tracked message,
the client creates and tracks one. These notifications are not durable across bot crashes.

In simple mode, `scripts/install.sh` initializes the backend schema, bootstraps
each Telegram administrator listed in `ADMIN_IDS`, creates an adapter credential
on first install, and starts the backend, worker timer, and aiogram client.
It disables an existing retired `node-plane.service` when activating
`node-plane-telegram.service`. There is no PTB runtime or fallback in this
repository; rollback targets must contain the backend and aiogram client.
The adapter credential path is stored in the shared `.env` and preserved across
reinstalls. `NODE_PLANE_BACKEND_URL` defaults to `http://127.0.0.1:8080`.
For a manual installation, the individual unit installer remains available:
`scripts/install_telegram_client_systemd.sh BASE_DIR SHARED_DIR --activate`.
Test the new flow with a disposable node before using it for production access.
The simple-mode updater and health check recognize the active new service;
updating it does not run the legacy registry-based agent rollout.

The control message ID and callback tokens currently live in process memory.
Member navigation has no idle expiry and failed actions can be retried. Its
token cache is bounded to 10,000 entries. After a restart or cache eviction,
pressing an old button re-authorizes the caller and refreshes the current message
with Home (or the language picker if needed), without replaying the lost action
or requiring `/start`. Buttons from another account are rejected. Backend
authorization and config-artifact expiry remain independent of navigation.
Language selection acknowledges the chosen language immediately, reuses the
account returned by the preference update and loads independent home reads in
parallel. The language-to-home transition has a 12-second deadline and reports an error rather than
waiting for multiple sequential HTTP timeouts. Callback acknowledgement has a
three-second timeout and its failure does not prevent screen navigation. Logs
report language-selection transition duration. The access gate uses a Rich
section with an embedded primary Request access button and separate settings.
The administration home shows a native compact table of stored counts with
conditional attention actions and embedded Management, Requests and System
buttons. Optional overview/title reads are concurrent and bounded at three
seconds; unavailable summary data does not prevent navigation. Request list,
review, search and notifications use embedded actions, collapsed identifiers,
and localized empty/error states. Search Back retains filter/page context.
Native tables retain labeled rows in the plain-text fallback. See
[DEVELOPMENT_PLAN.md](../../DEVELOPMENT_PLAN.md) for remaining development work.
Profile lists use one clickable `name · status` row per profile. Landing cards
show a short summary and Access/Edit actions. Management contains account roles
and deletion; Technical details and operations are nested further inside it.
Screen/section/server headings use H1/H2/H3. Access tables live in the editor. Access editors and creation load all registry
pages and display 10 servers per page. Grant/Revoke all and regional buttons
modify only the draft, including other pages; Save is required. Region choices
are explicit snapshots: future servers require another Grant press and Save.
Bulk Grant skips disabled servers, while bulk Revoke also removes disabled or
obsolete grants in its scope. Opaque region tokens keep Unicode callbacks short
and prevent stale buttons from selecting a different region. Edits preserve the
starting profile revision; conflicts retain the draft instead of overwriting a
concurrent change. Administrator profile traffic uses a dedicated management-authorized summary
and a separate regional server view. Global admin policy controls collection
and visibility; member opt-ins have been removed.
Back-to-menu navigation reuses the last approved home presentation in the
current user's FSM context instead of requesting account and bot title again.
This is only a menu snapshot, never an authorization cache: opening destinations
and issuing configs still use authenticated backend calls. `/start` reloads the
menu, and access decisions invalidate the recipient's snapshot. Pending access
screens and missing snapshots are always loaded from the backend.
Config screens put collapsed QR/help blocks above a Show/Hide URI button.
The full monospace URI appears below its button outside details; downloadable
files follow outside details for iOS compatibility.
AWG includes both downloadable `.vpn` and `.conf` files. Import labels use
`Server name Protocol [Transport] · Profile name`; AWG passes this description
to the node's converter and VLESS stores it in the URI fragment.
Server sections provide protocol buttons directly, without another selection
screen. Both Get config and Profile sort servers by region and name, and place
each region above its servers as a heading. Server headings prefix names with
their configured flags, without repeating the region. Administrator grant,
problem-node, rollout and active-alert lists use the same flag/name formatter.
Member lists have dividers between servers and before Back, and show ten servers per page;
arrow-only navigation and the page number appear only with multiple pages.
Profile pagination lives inside its server accordion, keeps it open after a page
change, and retains the account/statistics section above it. Get config remembers
the page when returning from a protocol or config screen. Backend cursor pages
are combined before sorting so lists beyond 100 servers are not truncated.
VLESS offers XHTTP and TCP in a single row. The Profile screen
shows account details and full statistics immediately, followed by a collapsed
read-only list of granted servers; config selection lives in Get config.
Language and announcement sound settings use a pair of buttons in one row;
only the selected value has the primary color, without a separate value label.
The final server in the Profile accordion has no trailing divider; one divider
outside the accordion separates Back from the rest of the screen.
Navigation sits at
the bottom as link-style buttons, separated from content by a divider. When Rich Message delivery is rejected by
Telegram, the screen falls back to plain text with equivalent inline navigation;
files and QR are then sent separately. All member text is localized in RU/EN.
A real Telegram client acceptance pass on Android, iOS, and Desktop is still
required for rendering and document/QR behavior.

Profile Edit also supports preset durations, an explicit UTC expiry date, and
no expiry. The form uses a captured revision and idempotency key. The backend
worker queues runtime revocations when expiry is reached, retaining grants and
identities for later extensions and preserving blocked-node recovery rules.


Access-decision notifications retain an isolated in-message setup wizard. Approval
shows Edit profile and Close; rejection shows Close. The grants/duration review
is saved atomically, and the resulting overview offers Edit and Close. Main-panel
FSM state remains independent. Reply to the notification when entering a custom
expiry date. Ordinary member profiles support finite or permanent duration;
administrator profiles always have permanent duration enforced by the backend.

## Presentation invariants

Keep a single edited control message for normal navigation. Use H1/H2/H3 for
screen, section and item hierarchy; short native tables for comparable facts;
collapsed technical details and artifact groups; monospace copyable URIs.
Hidden content is not an authorization boundary. Keep status, failures and
required decisions visible, with destructive confirmation where applicable.

Use exactly one divider before Back, return to the actual parent and preserve
search/page state. Pair related short actions in rows, including wizard Back
with Next/Skip. Group flagged servers by region and paginate at 10 items with
arrow-only navigation shown only when needed. Keep complete RU/EN coverage and
labeled plain-text fallback. Do not make ordinary navigation run fleet probes.
