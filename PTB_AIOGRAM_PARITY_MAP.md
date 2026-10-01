# PTB → aiogram UI parity map

Baseline: repository `dev` at `v0.4.3-alpha.18` (2026-09-29). This is an implementation inventory, not a proposal to retire old features. **Every PTB screen and button is a parity requirement**, including source-defined screens that the current PTB keyboard does not expose and operational tools that may be scheduled later. Extra aiogram screens may remain, but they cannot replace a PTB action with a different outcome without an explicit product decision.

Source of truth: `app/utils/keyboards.py`, `app/ui/{menu,user_views,admin_views}.py`, `app/handlers/{user_common,user_getkey,user_profile,admin_wizard,admin_server_wizard,admin_commands}.py`, and `app/routers/callback_router.py`. New-client comparison: `app/telegram_client/routers/*.py`, `app/telegram_client/screens.py`, `app/telegram_client/backend.py`. Backend capability check: routes in `app/backend/http_api.py`. Button labels below are shortened English descriptions; callback names identify the exact PTB action. A button is **partial** if it exists but its result, visibility rule, data, or return path differs. **Missing** means there is no reachable equivalent. **Broken** means aiogram renders a button without a matching handler. **Present** only means a comparable path exists in code; it is not evidence of live Telegram/node acceptance.

Implementation invariant: every user-facing string in the replacement client must
come from the shared RU/EN catalog. Add both translations with each screen,
button, notification, and error while restoring parity; do not add one-language
text as a temporary shortcut. Locale preference belongs to the backend identity
record and the first-run/settings language screens must allow the user to change it.

## Scope update and alerts checkpoint (2026-09-30)

Command inventory and implementation are deferred for a separate product review.
Historical command rows remain reference material; legacy aliases are not an
automatic parity requirement. Screen/button parity remains in scope.

The Alerts block now has backend policy and aiogram screens for enable/disable,
5/15-minute intervals, recovery notices, active conditions and delivery results.
The backend worker performs read-only inspections after queued mutations, using
at most four concurrent agent requests and a five-second driver RPC timeout.
Only enabled nodes with applied settings are monitored; new nodes, pending
settings, maintenance, drains and cleanup are excluded.

The agent reports available-memory RAM usage, free space on the runtime filesystem,
1-minute load and CPU count. Fixed initial thresholds are disk free below 10%,
RAM usage at least 90%, and load at least twice the CPU count. Observations remain
separate from desired state: monitoring never enables nodes or applies settings.
Older runtime bundles without an explicit trustworthy inspection marker report
unknown service/resource measurements until runtime synchronization.

Persistent alert state emits notifications only for condition transitions.
Unknown readings preserve previous alarms; driver/control-plane failure does not
fabricate an outage on every node. Resolved notices are optional, and removed
protocols retire their service alarms without claiming recovery. Admin eligibility
and node availability are checked again before delivery. Delivery claims use the
same no-replay-on-uncertainty contract as announcements, with localized Rich
Messages and safe plain-text fallback. Polling alternates alerts and announcements
so one queue cannot starve the other. Node deletion removes its monitoring state
and notification history; snapshot restoration excludes alert state/outboxes.

Automated verification covers transitions, unknown/malformed metrics, driver and
agent failure, maintenance exclusion, recipient revocation, queue interruption,
policy and RU/EN screens. Live agent/Telegram/PostgreSQL acceptance remains open.
Factory reset/full removal is implemented in the October 1 checkpoint below. Traffic telemetry
is implemented in the checkpoint below. Commands will be reviewed separately.

## Announcements implementation checkpoint (2026-09-30)

- Admin menu exposes announcement overview, compose, preview, edit through Back,
  explicit Send and durable delivery results. Composition retains the draft;
  Back and Preview/Send share a row where both actions are available.
- The backend validates the message and snapshots approved Telegram recipients,
  excluding the sender. Pending/disabled accounts and accounts without Telegram
  identities are excluded. Eligibility is checked again before delivery.
- Stable command keys prevent duplicate broadcasts after double taps. A separate
  async Telegram transport polls persisted deliveries after client restart; leaving
  the compose/result screens does not cancel an admitted broadcast.
- The result distinguishes queued, sending, sent, failed, skipped and unknown.
  Telegram does not support idempotent message sends: expired or ambiguous claims
  become unknown and are not automatically replayed. Explicit Rich Message
  rejection can safely use a plain-text fallback without Markdown.
- Member settings persist announcement sound preference per backend account;
  delivery applies the current preference and recipient locale. RU/EN strings
  cover the new screens, buttons, notifications and errors.
- Backup restore is blocked while announcement delivery is pending; restore
  excludes and clears the announcement outbox so old broadcasts cannot replay.

Implementation and automated checks are complete for this slice. Live Telegram
acceptance remains required, including blocked recipients and client restarts.
Alerts and telemetry are implemented in later checkpoints. Factory reset/full
removal is implemented below; final live acceptance remains open and commands are deferred.

## Node installation and maintenance implementation checkpoint (2026-09-30)

The tables below retain the original comparison baseline. For the node block,
this checkpoint supersedes their “Missing”/“Broken” implementation statuses:

- Node cards and lists expose saved readiness/provisioning summaries; cards also
  query live agent/container state, runtime version/commit and drift. Agent
  absence and connection failure use localized explanations.
- Installation checks Docker and reusable protocol configs. Keep-config reinstall
  is offered only when all selected protocol configs are reusable, and the agent
  checks again before execution. Bootstrap and clean reinstall are durable worker
  jobs; progress does not depend on keeping the Telegram screen open.
- Settings have General/connection/protocol, Xray and AWG sections, including
  notes, fingerprint, interface and I1 preset. Settings are desired state until
  explicitly applied; Apply belongs to the settings root.
- Diagnostics, port check/open, runtime inspection/sync, environment/Xray repair,
  access reconciliation and AWG entropy inspection/regeneration have backend
  operations and localized result/Back screens. Runtime sync is conditional on
  drift; Xray repair is conditional on the selected protocol.
- Mutations carry stable command IDs and node revisions. Interrupted native
  operations require journal recovery or explicit retirement after agent restart;
  they are never blindly replayed. Rebuilds fence config issuance and schedule
  fresh profile reconciliation before issuing new artifacts.
- Full deletion has one card entry. The worker revokes access, removes runtime
  and the agent, verifies host cleanup, then removes the registry entry. Runtime
  cleanup retaining the node/agent is a separate maintenance operation. Registry
  removal alone is offered after connection/verification failure.
- Agent installation checks release binaries before local build. Missing Rust
  offers an explicit installation confirmation; resource failures explain that
  release binaries are needed instead of displaying console logs.
- New node screens/actions have RU/EN catalog entries. Automated checks cover
  job idempotency, interruption, config fencing/reconciliation, cleanup and
  worker-driven removal. This is implementation verification, not live acceptance.

Deployment prerequisite: verified SSH removal needs an independent root-access
key in `NODE_PLANE_REMOVAL_SSH_KEY`, distinct from the bot key being removed.
Local verification needs the bot public key through `SSH_KEY` or
`NODE_PLANE_BOT_PUBLIC_KEY_FILE`. Missing credentials block deletion before
revocation or runtime cleanup.

Remaining outside this block: backups/reset/alerts, the complete
command-alias inventory, and the final cross-section live acceptance matrix.
Node live acceptance must still exercise first installation on a clean host,
both reinstall variants, interrupted-operation recovery, local/SSH full removal,
and returning working AWG/Xray configs after reconciliation.

## Updates implementation checkpoint (2026-09-30)

The Updates rows below retain the original comparison baseline. Their missing
implementation items are now covered by the backend and aiogram update screens:

- Paginated version catalog with current/upgrade/downgrade/blocked markers,
  compatibility explanations, an explicit confirmation and Back navigation.
  The backend reloads the catalog and checks the selected branch/ref before
  accepting a command. Dev HEAD is pinned to a full commit for execution.
- Latest-version updates use the same confirmation and durable command contract.
  Double taps reuse one command key. The worker schedules the existing systemd
  updater; an uncertain launch is blocked rather than replayed.
- Branch and dev tag/HEAD selection, auto-check toggle and hourly backend-worker
  checks. A failed upstream check does not prevent provisioning/cleanup work.
- Fleet overview displays actual driver/agent binary commits separately from
  runtime commits. Current components hide mutation buttons; unreachable
  components remain unknown. Installation success is verified against the
  expected commit before a batch item is marked successful.
- Durable driver/agent rollout and runtime-sync batches use the backend node
  registry, persist individual results, and continue after leaving Telegram.
  Driver-only rollout supports an empty node registry. Runtime synchronization
  reuses the native node-job journal/recovery contract.
- Updates shows the latest task/progress/result; release cleanup keeps its
  eligibility/count/size checks. All added screens/messages have RU/EN entries.

Live acceptance remains necessary: install a selected release on a systemd
host, return to the bot after stack restart, confirm that fleet update buttons
vanish after successful rollout, and test partial failure on an unavailable
node. This checkpoint records implementation and automated verification; no
release or live system update was performed as part of it.

Follow-up verification (2026-09-30): the access-request policy PATCH contract now
accepts the per-admin notification preference sent by the settings screen.
QR delivery also respects navigation: an artifact read completed after Back
does not send a photo; a photo sent while navigating away is deleted and does
not replace the destination screen. Both QR cases have async regression tests.

Implementation progress (2026-09-29): the first parity slice is underway. The
member config path now groups protocol selection, Xray transport selection, and
AWG file-format selection; successful config delivery avoids re-sending the
artifact when the control screen refreshes. Telegram display name, username and
language are recorded by the backend and included with pending access requests
and admin account records. The shared RU/EN catalog and persisted member locale setting
are now used by the member and access-request screens. New users choose a
language on first start; config files and QR messages are tracked and deleted as
the user navigates. Remaining parity work includes broader profile/node-first
navigation and translating every other aiogram screen before the shared
interaction section is complete.

The admin profile flow now creates the profile and selected grants in one
backend command, so provisioning cannot start with an incomplete selection.
Its profile list uses backend-wide search and cursor pagination. Profile cards
show grouped grants and the latest provisioning operation. Grant editing uses
a draft and one explicit Save action. Profile rename is available from the edit
menu. These paths use the shared RU/EN catalog; scheduled expiration and wider
admin-screen parity remain.

Profile deletion now has a confirmation screen and a durable backend revocation
command. The profile disappears from member access immediately; administrators
can still see it while node cleanup is pending or blocked. It disappears from
the admin list only after the delete operation succeeds. This replaces PTB's
best-effort removal, which could erase the local record despite remote errors.
The result still requires live worker/node acceptance testing.

An admin Status screen now reports backend version, enabled nodes, profile and
request counts, and nodes with blocked tasks. It links to those nodes and to
Requests, Nodes, and Profiles. The figures come from durable backend state;
live agent reachability and runtime drift still require dedicated probes and
remain open parity work.

Admin Settings, SSH key summary/guide, Updates overview, and old-release
cleanup now use RU/EN text. The Updates screen shows branch, dev track,
install mode/source, auto-check state, last check, current/remote labels and
run status. Release cleanup shows eligibility, counts, size and active release;
Run appears only when supported and there is something to remove. The backend
cleanup routes now call the existing release-cleanup service instead of missing
functions in `updates.py`. Branch selection and auto-check toggle are now
backend-backed. Changing branch or dev track clears the previous check result
so an update from another track is not offered. Version selection,
driver/agent rollout, backup screens and the other settings items remain open.

The admin Nodes list now uses backend cursor pagination and Unicode-aware
search across node key, title and region, including records beyond the first
100. Search and page context survive returning from a node card; the list
offers a clear Show all action. Readiness markers, provisioning counts and
the creation-wizard parity below remain open.

The node creation wizard now validates each text step, retains entered values
when moving Back, and presents a final review of key, name, region, SSH/local
connection, public address, protocols and default Xray transports. Save only
registers the node. A separate action starts agent rollout after registration;
the node card remains available without an agent. These wizard screens use the
RU/EN catalog. The backend now persists the local/SSH agent connection and
SSH target alongside the node, returns them on reads, and uses them for later
agent setup. Existing nodes without this metadata remain readable and keep
both setup choices. The node settings screen now lets an administrator switch
between local and SSH connections and edit the SSH target; the backend rejects
agent rollout against a different saved target. The relevant settings screens
and validation messages use the RU/EN catalog. Readiness-aware setup menus and
full installation parity remain open.

The Apply action now lives on the root node-settings screen and returns there
after success or failure. Failed scheduling and temporarily unavailable task
status have localized, recoverable screens. Protocol toggles now send a valid
idempotency key; previously they passed `None` and could not be accepted by
the backend. The Telegram flow tests were updated for the explicit node Save →
Agent setup sequence and draft → Save grant editing. Access-request service
tests now account for the identity fields added to admin list responses.

The node card now reads a fast backend summary of persisted settings revisions
and profile access task outcomes. It labels these as saved state, never live
health. Agent reachability remains an explicit Probe action, whose success and
unavailable/unconfigured/driver-error screens are localized and avoid raw RPC
diagnostics. Container service readiness, live traffic, and reinstall choices
still need dedicated verified operations.

The Probe screen now links to live agent diagnostics through the backend. It
reports Docker availability, runtime directory presence, Xray/AWG config
validity, and runtime version without exposing agent paths or raw gRPC errors.
The backend rejects the driver's legacy saved-state response when no agent
target exists. Container process health and actual VPN connectivity are not
verified by this endpoint and remain separate parity work.

The node protocol and maintenance screens now use RU/EN text, including drain,
verification, and registry-only removal confirmations. Maintenance mutations
show a recoverable error screen if the backend cannot confirm completion; they
no longer expose raw backend codes or drop the admin into an unhandled error.

The member Account action now opens an owned profile screen with Telegram
identity, active/frozen status, optional expiry, and node/protocol access.
Statistics show the profile creation date, node/protocol counts, successful
config issuance count, and latest issuance time from a backend-owned read
model. The backend distinguishes active, frozen, and expired profiles, and
keeps profiles independent of Telegram identities. Older
profiles without a recorded creation date show an unknown date. Traffic
telemetry and notification preferences still need separate backend support.

AWG QR generation now uses the import payload without the `vpn://` prefix, as
the PTB client did. QR Back returns to the issuance screen; tracked attachment
messages are removed on navigation and can be sent again when returning to
that screen. The `.conf` artifact no longer offers an Amnezia QR button. A
successful `.vpn` issuance now shows protocol-specific import guidance and the
full `vpn://` URI in a collapsible Rich Message section. AWG and VLESS files
include both server title and profile name in their filenames.

Config issuance polling now stops updating the control screen after Back or
another member action. Late artifact reads and backend errors are discarded
when their screen is no longer active; a file whose send completes after
navigation is removed. Pending results expose a localized Refresh status action.

Access requests now use backend cursor pagination and search by Telegram ID,
name, or username. The request card loads by ID and shows identity and timestamp.
Admin notifications offer direct Approve and Reject actions; stale decisions
return to the current request list. After the last pending request is decided,
the control screen returns to the admin menu.

Access-request policy now has backend-owned enabled/message settings. The admin
can toggle new requests and edit the text shown to users without access; request
creation enforces the toggle in the backend. The member home hides the request
button and displays the configured message when requests are disabled. Admin
notification preferences are stored per administrator and suppress new-request
messages when disabled. Request reminder behavior remains open.

The bot menu title is now a backend-owned setting with an administrator edit
screen; the member home, admin menu, and first-run language screen render the
configured title instead of a fixed label.

The installation menu now checks the backend's settings snapshot before showing
Install protocols. Incomplete settings lead to the settings screen; an applied
revision leads to Probe; applying or blocked settings do not offer a duplicate
apply command. The agent setup result now links to the real settings flow. Its
old `Refresh runtime` button had no handler and was removed; runtime sync still
needs its own durable backend operation and drift check. This does not yet
replace the PTB bootstrap/reinstall choices or explicit Docker installation.

Both creation wizards now use Back instead of Cancel on their steps. Back sits
beside Skip, Next, Review, or Save where those forward actions exist. The node
wizard preserves entered fields when stepping backward. The profile wizard
preserves selected grants on Back and has a review screen before the final
Save; Next from a node's protocol picker opens that review. Returning from the
first profile step to the account card clears its draft state.

## 0. Shared interaction contract

| PTB behavior | aiogram today | Parity work |
|---|---|---|
| A control screen is normally edited in place; transient input messages are removed. QR/file screens use separate Telegram messages and delete their old artifacts on navigation. | `render()` edits the remembered control message with Rich Message, then falls back to plain text. File/QR messages are sent separately but not tracked or removed. | Specify one control-message owner per chat/session. Track every artifact message, clean it on back/new issuance/revocation, and verify `edit_message_text`/Rich Message fallback and back navigation on mobile. |
| PTB uses RU/EN locale, first-run language gate, and locale-aware button/status/error text. | First-run choice, persisted backend locale, and a 509-key RU/EN catalog are present; some admin screens still contain hard-coded text or missing translations. | Finish catalog coverage for every screen, notice, error, and button. Add the settings language switch and verify all navigation with both locales. |
| PTB buttons persist until the message changes and mostly use deterministic callbacks. | Member buttons are in a process-local 15-minute token map; old buttons stop after restart. Admin callbacks are packed IDs; some raw strings remain. | Decide persistence/expiry deliberately, provide a useful recovery path on stale callbacks, and test admin/member ownership, double taps, restart, and a second bot process. |
| PTB guards privileged actions and checks frozen/revoked access again on every config/QR/file callback. | Backend authorizes API operations; many admin callbacks rely on backend errors. Artifact GET rechecks live state. | Keep authorization in backend; show controlled UI errors and current navigation rather than tracebacks. Recheck access for old Telegram buttons and purge old artifacts. |
| PTB shows progress, short success/failure summaries, and usable back buttons for long node operations. | Several aiogram actions wait/poll inline; some backend failures escape handler; one card fetch used to fail silently. | Standardize queued/running/succeeded/blocked/superseded screens, refresh, retry semantics, concise diagnostics, and no accidental duplicate command. |
| PTB paginates server/profile/request/version/backup lists, and offers search where applicable. | Node, admin profile, and request lists now have backend cursor/search support; member owned-profile list and several settings lists still fetch one page. | Finish pagination for remaining lists and test >1 page plus callbacks from old pages. |

## 1. Entry, access gate, and member home

PTB: `user_common.py:start_cmd`, `user_profile.py:on_menu_callback`, `keyboards.py:kb_main_menu`. New: `telegram_client/routers/user.py`.

| PTB screen/button | PTB result and visibility | aiogram today | Exact parity task |
|---|---|---|---|
| `/start` first visit → RU / EN | Saves Telegram identity; first visit asks for language before home. Admin gets a profile automatically. | Resolves identity, offers RU/EN on first visit, persists locale, and uses backend title on the language screen. | Automatic admin profile creation remains missing. |
| `/start` again; `menu:main` / Back | Renders configured bot title and access-aware main menu. | Reads backend title for member and admin home screens. | Present. |
| No access: `menu:request_access` | Button is hidden when requests are disabled; disabled screen shows custom access-gate text. Pending request has its own state. | Backend policy controls button visibility and custom gate text; pending state remains distinct; disabled accounts are rejected by authorization. | Add richer rejected/disabled explanations and explicit reapply policy. |
| Main: Get key (`getkey:menu`) | Directly lists accessible servers for the single bound PTB profile. | Opens a list of owned profiles first. | Preserve the server-first PTB path for a single profile; retain a profile chooser only when multiple owned profiles make it necessary. |
| Main: Profile (`menu:profile`) | Detailed profile identity, status, access per server; Statistics and Back. | `My account` shows owned profile(s), Telegram identity, active/frozen/expired status, optional expiry and grouped node/protocol access. Statistics are backend-backed. | Traffic telemetry and notification preferences are backend-owned and implemented; live acceptance remains open. |
| Main: Settings (`menu:settings`) | Language, announcement sound, conditional traffic telemetry, Back. | Language, announcement sound and conditional traffic-consent controls are implemented. | Present; traffic consent can still be revoked when global availability is disabled. |
| Main: Admin (`menu:admin`) | Admin dashboard or first-node setup invitation. | Basic admin menu, no first-node invitation. | Add first-node setup gate (local / remote / later) and admin dashboard parity. |
| `/whoami`, `/version`, `/getkey` | Returns Telegram identity, bot version, or invokes start/home respectively. | All three commands exist: identity is shown locally, version comes from the backend, and Get key follows the start/home flow including first-run language choice. | Present. |

### Member profile and statistics

| PTB button/screen | aiogram today | Exact parity task |
|---|---|---|
| Profile → Statistics (`menu:profile_stats`) | Missing. | Shows profile creation date, node/protocol counts, Xray/AWG counts, successful config issuance count and latest issuance. | Opt-in Xray/AWG totals and sample times are included; unknown/stale observations are explicitly marked. |
| Profile → Back | Account info goes straight home. | Profile Back returns home; Statistics Back returns to profile. | Present. |
| Profile identity/status/access | Account info lacks profile name, username, Telegram ID, access summary. | Owner-only backend summary exposes profile data, while Telegram identity remains optional and separate from profile ownership. | Existing profiles without a creation date display `—`. |
| Settings → Language → RU / EN → Back | Language picker is available from member Settings. | Persist locale and redraw current screen using selected language; first-run choice has no Back, settings choice does. |
| Settings → Silent announcements toggle → Back | Implemented: account preference and delivery behavior. | Live acceptance of silent delivery remains. |
| Settings → Traffic telemetry toggle (only if globally available) → Back | Present. | Backend account consent, conditional visibility, global availability and member summaries are implemented. Existing consent remains revocable while globally unavailable. |

## 2. Member configuration issuance

PTB: `user_getkey.py`, `ui/user_views.py`, `utils/keyboards.py`. New: `telegram_client/routers/user.py`. Backend issuance/artifact APIs already exist.

| PTB screen/button | PTB result | aiogram today | Exact parity task |
|---|---|---|---|
| Get key → server rows → Back | Lists only granted nodes, groups connection methods under each node, Back to home. | Owned profiles → nodes; node screen lists transport-flattened actions. | Preserve the hierarchy and readable server/method labels. Provide optional profile chooser only when needed. Honor inactive/frozen/no-method screens. |
| Server → AWG | Refreshes live AWG peer, shows import instructions and `vpn://` URI on an AWG screen. | `.vpn`/`.conf` choices issue immediately; success sends a named file, shows format-specific guidance, and `.vpn` adds a collapsible URI. | Add a pre-issuance AWG overview and return to it after QR/file use. Keep backend live-artifact freshness checks. |
| AWG → Show QR | Encodes the Amnezia payload (without `vpn://`), sends photo with Back; Back reopens AWG screen after refresh. | AWG `.vpn` QR strips `vpn://`; Back returns to issuance result. `.conf` does not show QR. | Add pre-issuance AWG overview and return to it after fresh artifact validation; current path stays on issuance result. |
| AWG → Download `.vpn` | Sends named file and import caption with Back to AWG screen. | Sends `.vpn` after selection; filename includes server title and profile; result has import guidance and expandable URI. | Add explicit result actions/back to AWG overview and artifact cleanup. |
| AWG → Download `.conf` | Same as `.vpn` with `.conf` for compatible clients. | Sends `.conf` with server/profile filename and compatible-client import guidance. | Add explicit result actions/back to AWG overview and artifact cleanup. |
| Server → Xray → XHTTP (primary) / TCP (fallback) | Separate transport picker; Xray screen shows live VLESS link and import guidance. | Separate TCP/XHTTP picker; selecting a transport queues issuance, then shows VLESS link in collapsible details, sends a named `.txt` file, and gives protocol-specific import guidance. | Add an import screen before issuance only if needed; current path goes directly to a usable result. |
| Xray → Show QR → Back | Sends QR with caption; Back returns to same transport's Xray screen, then Back to transport picker, then server. | QR is shown from issuance result; Back returns to that result. Its Back returns to transport picker, then node. | Add import caption/instructions and verify freshness on the full path. |
| AWG/Xray error screens | Friendly, localized error; never hand out stale config after refresh failure. | Backend codes are mapped to localized access/missing/service/retry text; blocked/superseded issuance shows a fresh-config recovery prompt. | Add protocol-specific recovery guidance and distinguish more issuance failure causes. |
| Issuance waiting/Refresh | PTB actions run to result; no explicit polling screen. | New pending screen with Refresh, 15-second polling loop. | Keep this new async screen if useful; ensure no repeat file sends or duplicate issuance on refresh/back, and handle timeout/worker unavailable. |

## 3. Access requests and admin notifications

PTB: `user_profile.py` request dashboard/card/search/decision and `request_access`. New: `admin_requests.py`.

| PTB screen/button | aiogram today | Exact parity task |
|---|---|---|
| Member Request access / pending / rejected / globally disabled | Basic request/pending handling only. | Enforce the global switch and custom gate, status-specific text, idempotency, and reapplication policy. |
| Admin notification: Approve / Reject / Requests | Direct Approve/Reject and Review buttons. Decision opens the pending list or admin menu when empty. | Present; notification actions reset stale list/search state. |
| Request dashboard: user label, paging arrows, current page, Search, Back | Backend cursor pagination and search by Telegram ID, username, first/last name; preserves search and page context. | Present. |
| Request card: identity, username, full name, request timestamp/status; Approve / Reject / To list | Loads by request ID and shows user name, Telegram ID, username and timestamp; only pending requests expose decisions. | Present; stale requests return to the current list/page. |
| Decision → user notification and pending list/admin menu | Decision refreshes the current cursor/search page, or opens admin menu when none remain; requester notice uses their stored locale. | Respect admin notification preferences and verify stale/double-click behavior. |
| Admin request notification preference | Per-admin toggle in access policy settings; notification delivery checks it. | Present. |

## 4. Admin home and profile administration

PTB admin menu: `kb_admin_menu` and `user_profile.py`. Profile wizard: `admin_wizard.py`, `ui/admin_views.py`. New: `user.py:show_admin_menu`, `admin_profiles.py`.

### Admin home

| PTB button/screen | aiogram today | Exact parity task |
|---|---|---|
| Initial setup: Local / Remote / Later | Missing. | Show when no node was set up; each choice opens the appropriate create flow; Later persists dismissal. |
| Status / Requests row | Requests present; Status absent. | Add status overview with version, node/protocol readiness, profile totals, pending requests, runtime drift and recommended actions. Add `Requests`, `Problem nodes`, `Sync runtimes` conditional buttons and their screens. |
| Servers / Profiles row | Both present. | Restore status indicators, counts, hierarchy and navigation in their sections. |
| Announcement | Implemented; see the announcements checkpoint above. | Live acceptance: compose → preview → Back to edit / Send; approved recipients, sender exclusion, sound preference and truthful delivery counts. |
| Admin settings / Back | Present, but settings are a small subset. | Complete section 6 below; Back to member home. |
| Extra Accounts | New-only screen. | Keep as additive account management, but do not use it as a substitute for PTB profile dashboard and create flow. |

### Profile list, create, edit, delete

| PTB button/screen | aiogram today | Exact parity task |
|---|---|---|
| Profiles dashboard: each profile, page arrows/current page, conditional Search, Back | First 100 profiles, Search over this page, Back. | Cursor pagination and server-side search; return to same page and preserve search context. |
| Profile card: access grouped by server, provisioning/failed state, frozen/active; Edit / To profiles | New card shows name, ID, frozen/active, Grants, Freeze. | Add grants grouped by server/protocol and worker provisioning status; card Edit opens edit menu, Back returns to prior page. |
| Create profile → name → server list → per-server protocols → Next/Save → result | New path is Accounts → select account → name → create empty profile → add each grant separately. | Restore a cohesive create wizard, preserving separate backend account ownership. Offer account selection where necessary, then draft all server/protocol choices and submit atomically or as a tracked command. Show per-node provisioning results. |
| Per-server protocol toggle/selected marker and Back | New per-protocol add/remove actions execute immediately. | Add draft selection and explicit Save for parity; provide selected markers, one-method shortcut and correct Back without losing draft. |
| Edit menu: Protocols / Status / Save / Delete / To profile | New card exposes Grants and immediate Freeze; no Save or Delete. | Restore edit menu, draft changes and Save result. Immediate backend mutations may remain internally only if UI still clearly communicates when state is saved/provisioned. |
| Status → Freeze/Unfreeze → Back | Direct toggle exists on card. | Restore status sub-screen; show active/frozen state and runtime revocation/provisioning outcome. |
| Delete → confirmation → delete everywhere → result | Missing; backend has no profile DELETE route. | Add backend deletion saga/revocation, confirmation and result screen; handle unreachable nodes without claiming cleanup succeeded. |
| Profile search by name, empty result, Back | Searches ID/name in first 100. | Server-side search across all profiles, results, empty state and return to dashboard. |
| `/createcfg`, `/changecfg`, plus `/add`, `/del`, `/list` shortcuts | Missing (only `/start`). | Restore command entry points or equivalent command aliases; keep their exact semantics documented and routed through backend. `/add` and `/del` are Xray-specific shortcuts in PTB, not general profile CRUD. |

## 5. Admin nodes: inventory, creation, card, installation

PTB: `admin_server_wizard.py`, including card/advanced/maintenance. New: `admin_nodes.py`.

### Inventory and create wizard

| PTB screen/button | aiogram today | Exact parity task |
|---|---|---|
| Nodes dashboard: readiness/provisioned counts, attention marker, page arrows, Search, New node, Back | Plain list, New node, Back. | Backend summary read model; readiness/provisioning markers, cursor pagination, search and filter-result return. |
| New node / initial setup Local or Remote | New node starts key entry; transport choice comes later. | Support both normal and preselected local/SSH entry paths. |
| Create: key → title → flag → region → transport → SSH target (if SSH) → public host → Xray/AWG choices → summary Save/Back | Similar fields but no final summary, no stepwise Back, flag skip differs; `Done` creates and immediately queues agent. | Restore step prompts with current values, validation/retry, Back at every step, final summary and explicit Save. Keep node registration separate from rollout/installation so the user can review and change settings first. |
| Transport Local/SSH and SSH target | New SSH target only accepts port 22 and strips `:22`; local path exists. | Show actual supported target constraints and root/host-key prerequisites. Expose transport/target in node read model so later edits and card are accurate. |
| Xray/AWG selection and Save | Both can be toggled; Xray transports are silently initialized to TCP+XHTTP. | Show exact protocol selection and configured Xray transports; preserve an explicit choice/default visible to user. |
| `/servers`, `/addserver`, `/serverwizard`, `/serverconfig`, `/setserverfield` | Missing. | Restore command shortcuts and responses through backend or document intentionally equivalent aliases. |

### Node card and bootstrap/reinstall

| PTB button/screen | aiogram today | Exact parity task |
|---|---|---|
| Card overview: infra/transport/protocol/host, runtime version/drift, Xray/AWG readiness, profiles ready/failed/attention, next action, notes | Only region, protocols, Xray transports, generic sentence. | Add backend node overview aggregating runtime, services, provisioning and notes; show real readiness/attention, not inferred success. |
| Card Probe → result → Back | `Probe` reads runtime observation and shows health/config-present. | Match diagnostic scope and friendly unavailable/not-installed-agent distinction; result Back to same card. |
| Card Bootstrap → conditional menu | New static install menu always offers local agent, SSH agent and Apply. | Conditionally inspect agent and Docker: if agent absent, offer setup; if Docker absent, Install Docker; otherwise Bootstrap or Reinstall based on actual runtime. |
| Bootstrap menu Install Docker → concise result → Back | No dedicated button; Apply installs Docker implicitly. | Restore explicit Docker install and status screen, even if Apply can also install it. Needs backend operation/API. |
| Bootstrap / Reinstall mode → Keep config / Clean reinstall / retry check / Back | No distinction between first installation and reinstall; Apply is used for both. | Add backend-backed config-presence check; offer preserve only when verified, clean mode when safe, retry on unknown. Implement distinct bootstrap/reinstall jobs and result screens. |
| Bootstrap/Reinstall result → Set up agent or Install Rust when appropriate | Agent rollout is separate, with status screen. | Preserve truthful partial success/failure, retry and resource checks; Rust installation prompt only if local build is actually required. |
| Card Set up agent (conditional) | Agent rollout under install menu; no conditional status on card. | Show only when needed/failed; report driver/agent versions and action result. |
| Card Advanced / Delete / To nodes | Settings / Maintenance / To nodes exist. | Restore one clear Advanced entry and card-level removal, with context-preserving return. |
| `/probeserver`, `/bootstrapserver`, `/diag`, `/sshkey` | Missing. | Restore command aliases over backend actions or an equivalent command UX. |

### Advanced: General, Xray, AWG, Apply

| PTB button/screen | aiogram today | Exact parity task |
|---|---|---|
| Advanced root: General / Maintenance, conditional Xray / AWG, Apply changes, Back | Flat settings fields, Protocols, Back; Apply lives on card. | Restore submenus and place Apply on advanced root only; show desired-vs-applied drift and clear un-applied warning. |
| General: title / flag / region / transport / target / public host / protocols / notes, Back | title/flag/region/public host/Protocols exist; transport, target, notes missing. | Extend backend node model and PATCH validation; preserve current-value prompts, `.` to keep current where PTB supports it, per-field save and Back. |
| Xray: host / SNI / fingerprint / TCP port / XHTTP port, Back | SNI, TCP/XHTTP ports, XHTTP path; host/fingerprint missing. | Add host and fingerprint controls, full current values, validation and apply lifecycle. XHTTP path is a useful extra. |
| AWG: public host / interface / port / I1 preset, entropy inspect / regenerate, Back | Only AWG port. | Add backend fields and read/operation APIs for AWG host/interface/preset/entropy. Show current effective values and result screens. |
| Protocol selection in General | New protocol/transport toggles save each click. | Preserve PTB's visible selection, constraints and Back; ensure removals refuse in-use grants and keep runtime state coherent until Apply. |
| Apply changes → result | New Apply on card polls operation but may surface uncaught backend errors. | Recreate clear success/blocked/superseded result and return to Advanced. New configs must wait until applied; confirm revisions and provisioning status. |

### Maintenance and removal

| PTB screen/button | aiogram today | Exact parity task |
|---|---|---|
| Maintenance root: Diagnostics / Ports / Runtime / Repair / Back | Maintenance is exclusively a removal/drain workflow. | Separate operational maintenance from destructive node removal; add the PTB submenus. |
| Diagnostics → result → Back to Maintenance | Probe is on card but shows narrower fields. | Backend diagnostics API with Docker, ports, config and service checks; detailed but concise localized result. |
| Ports → Check ports / Open ports / Back | Missing. | Backend jobs and results for port check/open, with target node and validation. |
| Runtime → installed version/commit/drift; Sync runtime only when needed; Back | Rollout success shows `Refresh runtime`, but **the button has no handler**. | Expose runtime version/commit and drift; implement real Sync runtime action and status. Remove/repair the currently broken callback. |
| Repair → Sync node env / Sync Xray runtime (only Xray node) / Reconcile profile access / Back | Missing. | Add backend/driver operations and status screens; preserve conditional Xray button. |
| Card Delete → confirm → full remote cleanup → remove registry on verified success | New multi-step drain/bind/cleanup/verify UI, plus registry-only. | Keep new safer saga but expose a single clear full-delete path and automate safe steps where possible. Show exact progress/failure; no claim of full deletion until agent, service, configs, and registry are verified gone. |
| Agent unreachable → Remove from bot only fallback → result | Registry-only is always visible under Maintenance. | Show fallback only after verified connection failure/timeout, with explicit consequences. Guard stale confirmations. |
| PTB full-cleanup submenu in source: runtime-only / cleanup+delete / remove SSH key | The PTB source contains `_full_cleanup_markup`, but `cleanupmenu:` is rewritten to `deleteask:` before its later handler, so this submenu is **not normally reachable** in the current callback flow. | Still record it as a desired historical screen per parity requirement; decide its placement while avoiding multiple confusing Delete entries. Distinguish cleanup-keep-node from full removal and SSH-key option. |
| New-only maintenance: bind target / drain / cleanup step / verify / registry-only | No direct PTB screen counterpart. | Preserve as implementation of full removal, but simplify user-facing hierarchy and return paths. |
| `/syncnodeenv`, `/setxrayserver`, `/syncxrayserver` | Missing. | Restore command shortcuts for their backend-backed operations. |

## 6. Admin settings, updates, backups, and destructive maintenance

PTB: `utils/keyboards.py` and `user_profile.py`. New: `admin_settings.py`. The new backend exposes only SSH-key, coarse updates/check/run, and release cleanup endpoints for this area; most items below require new backend models/routes.

### Admin settings root and request settings

| PTB button/screen | aiogram today | Exact parity task |
|---|---|---|
| Bot title → input current title → Back/saved | Backend title setting and edit screen; member home, admin menu and first-run language screen use it. | Present. |
| Requests settings → access-gate message / notify toggle / request enable toggle / Back | Separate pending-request list and policy screen; policy screen edits message and enable toggle through backend routes. | Add admin notification preference and verify all settings transitions. |
| SSH key → summary → Details guide → Back | New screen displays raw public key and a one-line instruction; no Details. | Add summary, copyable public key and full host authorization guide with exact back paths. |
| Cleanup system → local reset / reset with nodes / full remove bot / full remove bot+nodes; typed phrase/Back/result | Missing. New `cleanup` only deletes old release directories. | Add a separate, strongly confirmed factory-reset/full-remove workflow. Never conflate it with release cleanup or node removal. Backend must own the operation and report partial remote cleanup. |
| PTB alerts settings: enable / interval 5 or 15 min / resolved notices / Back | Implemented: backend monitor/policy and reachable aiogram settings; see checkpoint above. | Live monitoring and Telegram delivery acceptance remain. |
| PTB global telemetry toggle handler | No current PTB keyboard button points to `admin_settings_toggle_telemetry`; new client also lacks it. | Admin settings now expose Traffic statistics, global availability, interval and last batch result. |

### Updates

| PTB screen/button | aiogram today | Exact parity task |
|---|---|---|
| Updates overview: branch, auto-check, last check/status, install mode/source, current/latest versions, last run/error, driver/agent rollout state and commit | Only branch/current/latest/last status plus abbreviated error/log. | Restore all fields with backend read model and human-friendly statuses. |
| Check now | Present. | Match spinner/result/error and locale. |
| Auto-check on/off | Missing. | Backend scheduler setting and toggle. |
| Branch → main / dev / Back | Missing. | Backend branch setting, selected marker, branch-specific check and Back. |
| Versions → paginated list, current/upgrade/downgrade markers → confirm → Install / Back | Missing. | Backend version catalog, transition rules/warnings, confirmation and install target. Include dev HEAD/tag track and blocked downgrade conditions. |
| Update latest (only when supported and update available/running) | New Run only when update available; running uses Refresh. | Preserve condition, target selection and truthful running/result state. |
| Update driver/agents (only when needed/running) | Missing. | Backend rollout overview and job, compare desired vs deployed commit; show action only when needed. |
| Sync runtimes (only when drift exists) | Missing. | Backend list/confirm/batch operation and result. |
| Old releases → counts/sizes/retention → Run → result/Back | New cleanup screen shows only status/log and Run unconditionally. | Restore counts, size, current path, removable count, support/eligibility gating, result and Back. |

### Backups

| PTB screen/button | aiogram today | Exact parity task |
|---|---|---|
| Backups overview: last backup, count/size/status → Create / Restore / Settings / Back | Implemented: backend-native snapshot overview and aiogram screen. | Verify on a systemd deployment. |
| Create backup → duplicate/failed/success result | Implemented: durable idempotent create job, deduplication and results. | Verify on a systemd deployment. |
| Restore → paginated backup list → choose → metadata/warning → confirm restore → result | Implemented: paginated catalog, metadata, checksum/schema validation, confirmation and worker restore with prior revocation. | Verify restore against live PostgreSQL and agents. |
| Settings: scheduled backup on/off, intervals 6/12/24 h, keep 5/10/20, Back | Implemented: backend policy, worker scheduling and selected values. | Verify scheduled execution on a deployment. |

## 7. Cross-cutting backend and test work required by this map

The following are dependencies, not permission to omit screens:

1. **Backend ownership:** add read/command APIs for every PTB action missing in `http_api.py` (profile deletion/provisioning state; node diagnostics, bootstrap/reinstall/ports/repair/runtime sync/AWG entropy; member/admin preferences; status; announcements; updates catalog/branch/rollout; backups; factory reset/full removal). Keep the bot as a presentation adapter. Use idempotency keys/revisions for mutations and explicit operation resources for long jobs.
2. **Data contracts:** node `AdminNodeOutput` currently has no transport, SSH target, notes, Xray fingerprint, AWG interface/preset, runtime/service/provisioning summary; account/request outputs lack the full display identity shown on PTB request cards; profile output lacks PTB statistics/provisioning summary. Extend versioned response models before rendering those screens.
3. **Navigation inventory:** for each row above, write an acceptance fixture specifying visible buttons, hidden-button conditions, callback target, success/failure screen, Back destination, and stale-button behavior. Test more than one page, more than one owned profile, local and SSH nodes, both protocols, no agent, unreachable agent, first install, reinstall with/without existing config, frozen/revoked access, and restart between screens.
4. **Language and Rich Messages:** port RU/EN catalog, maintain `Screen.rich()` and plain fallback, verify real Telegram rendering on Android/iOS/Desktop. Large AWG data belongs in collapsible details only when Telegram supports it; keep explicit copyable URI, QR, `.vpn`, and `.conf` outputs.
5. **Command parity:** PTB registers `/start`, `/whoami`, `/getkey`, `/version`, `/add`, `/del`, `/list`, `/servers`, `/addserver`, `/serverwizard`, `/serverconfig`, `/setserverfield`, `/syncnodeenv`, `/probeserver`, `/bootstrapserver`, `/diag`, `/setxrayserver`, `/syncxrayserver`, `/sshkey`, `/createcfg`, `/changecfg` in `app/main.py`. The new dispatcher registers only `/start`. Implement equivalent commands or explicit links into the same aiogram screens and backend actions; document arguments and permissions.

Suggested implementation order, without dropping any inventory item: (1) member navigation/config artifacts and request details; (2) profile CRUD/provisioning and admin status; (3) node creation/card/bootstrap/advanced parity; (4) maintenance/repair/removal; (5) settings/locales/announcements/updates/backups/reset; (6) all command aliases and end-to-end parity matrix. Each stage is complete only when every listed PTB button in that stage has an aiogram destination and tested backend effect, including error and Back paths.


### Traffic telemetry implementation checkpoint (2026-09-30)

- Admin traffic settings expose backend-owned availability and the last sampling
  result. Member consent is independent of Telegram identity, defaults off,
  remains revocable while globally unavailable and deletes that account's history
  on withdrawal. Enabling availability never opts accounts in.
- The worker samples at most 32 synchronized profile/node/protocol pairs every
  five minutes, rotating larger fleets with four concurrent requests. Frozen,
  expired, deleting, orphaned, unsynchronized and unconsenting profiles, pending
  node settings, drains, cleanup and restore are excluded.
- Driver forwards a read-only action without runtime deployment or legacy DB
  access. The agent checks requested identity, reads filtered non-resetting Xray
  StatsService counters or AWG peer transfer counters and returns only that
  profile's byte counters. Reads use the existing shared mutation lock and fail
  immediately if maintenance holds it. Runtime synchronization is required.
- First samples establish baselines. Container/boot epochs and counter decreases
  handle restarts/resets without negative totals or replay. Unavailable counters
  retain previous readings and mark results unknown. Pausing collection clears
  baselines, retaining totals but excluding the pause from later accounting.
- Final admission rechecks settings generations, owner, grants and revisions;
  even an off/on consent change while a request is in flight fences its response.
  Snapshots retain consent/policy but exclude usage and transient collection state;
  node retirement and profile deletion remove traffic rows.
- Profile statistics show localized Xray/AWG upload/download totals, collection
  start and last sample. Stale, paused or unavailable counters are explicit.
  Sampling is approximate: unsampled bytes before a restart can be lost; these
  totals are not billing or enforcement counters. Only byte counts are retained.

Automated tests cover protocol parsing, reset handling, consent/ownership races,
policy pauses, admission, unknown results, batch rotation and RU/EN screens.
Real VPS, Telegram and PostgreSQL integration acceptance remains open. The next
step is live acceptance of reset/full removal (implemented below); commands remain deferred.


### Controller reset and full removal checkpoint (2026-10-01)

The Cleanup system inventory row is now implemented in the native backend and
aiogram settings. This supersedes its original Missing status.

- Four choices match PTB: reset controller, reset including registered nodes,
  remove controller, remove including registered nodes. Release-directory cleanup
  and individual node removal remain separate operations.
- The backend returns a ten-minute, operator/principal-bound plan tied to the
  current account/profile/grant/node inventory and installer-owned paths. Exact
  typed confirmation and a stable command key admit one durable job. Back before
  admission leaves everything untouched. RU/EN screens include progress, individual
  node results, a recovery snapshot, retry, stop and final shutdown confirmation.
- Admission excludes active mutations and claimed notifications. New mutations,
  alert/announcement claims, automatic updates/backups and monitoring commits are
  fenced during cleanup. A failed step stays blocked until explicit recovery.
- A private pre-action snapshot is created before effects. With-node variants use
  the existing drain/revoke/remove/independent-verification workflow. Any blocked
  or registry-only removal prevents erasing the controller. Retry addresses failed
  items; stopping a job cannot undo nodes already cleaned or access already revoked.
- Reset deletes profiles, grants, nodes, other accounts, credentials, runtime
  journals, telemetry and managed SSH/mTLS keys. It intentionally preserves the
  confirming administrator's account/identities/locale, the active API client
  credential, shared environment, installation and one recovery snapshot so the
  new installation can immediately be configured again. Reset journals prevent
  replay of a consumed confirmation. With no node cleanup, runtimes remain unmanaged.
- Full removal prepares an independent systemd task outside the checkout. Telegram
  must deliver its final screen before acknowledging shutdown. The task stops the
  aiogram client, API, worker/timer and driver; removes owned unit/binary files,
  installation/shared paths and verified managed PostgreSQL container. External
  PostgreSQL data in the explicit Node Plane table inventory is cleared. Original
  source checkouts, unrelated containers/images, Docker packages and arbitrary
  host files are not swept. No Docker prune is used.
- An installer-written private ownership manifest is mandatory. Rerun the systemd
  installer to register existing installations. Repurposed units, unsafe paths and
  unverified PostgreSQL container ownership fail closed. A shutdown launch with an
  uncertain result is never replayed automatically; inspect the named systemd unit.

Policy, HTTP authorization/strict inputs, idempotency, blocked remote cleanup,
reset retention, backup failure, uncertain shutdown, owned-path validation,
script syntax, installer rendering and localized Telegram confirmation are covered
by automated tests. Actual destructive VPS/PostgreSQL/Telegram acceptance is still
required. Commands remain explicitly deferred for a separate design discussion.


### Supported commands (2026-10-01)

The agreed command set is `/start`, `/help`, `/id`, `/version`, `/status`.
`/status` reuses the existing administrator overview and backend authorization.
`/help` is localized; the Telegram command menu is registered in English and
Russian. Commands leave the active wizard and cancel pending artifact delivery.
`/whoami` and `/getkey` aliases are removed from the aiogram client. Other legacy
commands are intentionally excluded from parity by the product decision above.
