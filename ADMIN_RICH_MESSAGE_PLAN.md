# Administrator Rich Message UI plan

Status: Phase 1 and the core Phase 2 profile/access workflows are published in
`v0.4.3-alpha.30` and accepted during user testing. Administrator traffic
summary still needs a dedicated authorized backend read. Core Phase 3 server
screens are implemented and awaiting live UX acceptance; Phase 4 destinations
are being migrated, starting with Status. Server refinements and the SSH audit
are published in `v0.4.3-alpha.32`; the Status changes below are not in that tag.

Member UI baseline: commit `a68b2e2`, validated by 659 tests. This document
defines an incremental presentation redesign, not a replacement for the
existing functionality/parity checklist.

## Design rules

- Keep one control message. Navigation, forms, progress and results edit it.
- Use section headings, embedded button rows, dividers, collapsed details,
  monospace identifiers and downloadable diagnostics where useful. Reuse the
  existing `Screen`/`Section` renderer and plain-message fallback.
- Put actions immediately below the information they affect. Avoid a detached
  keyboard containing every possible action.
- Keep status, failures, pending changes and required operator decisions visible.
  Collapse identifiers, configuration details, logs and explanatory help.
- Use primary styling for the next recommended action and the selected option.
  Use destructive styling for deletion and rejection, with explicit confirmation
  for destructive workflows. Color supplements text; it is not the only signal.
- Separate the final Back link from content with exactly one divider. Back
  returns to the actual parent, retaining search and pagination state.
- Keep related choices in one row when labels fit: Active/Frozen, Local/SSH,
  XHTTP/TCP, Back/Next or Back/Skip. Do not force long labels into narrow rows.
- Paginate lists at 10 items initially. Show arrow-only navigation and a page
  indicator only when another page exists or the current page is not the first.
  Use cursor pagination without inventing a total page count the API cannot supply.
- Group server lists by region and prefix each name with its configured flag.
  Do not repeat the region beside every name. Preserve global ordering across
  pages; extend backend sorting when necessary rather than sorting each page alone.
- All interface text, empty states, errors, summaries and confirmation prompts
  must have English and Russian translations.
- Use native compact tables for comparable facts, rather than simulated tables
  made from monospace spacing. Prefer two or three short columns on mobile;
  use header cells, optional borders or striping where they improve readability.
  Keep actions outside table cells, beside the table's section. Long identifiers,
  URIs, secrets and narrative diagnostics belong in separate blocks. Plain-text
  fallback must retain each row's labels and values.
- Opening a summary must not implicitly run a fleet-wide probe. Distinguish
  stored state from a live check, show observation age when available, and label
  unknown state honestly. Independent reads may run concurrently.
- Collapsing a section is presentation, not authorization or lazy loading.
  Secrets require explicit authorized retrieval; expensive logs use an explicit
  Load/Refresh action. Never send private keys in hidden blocks by default.

## Navigation

```text
Administration
  Requests -> request review -> approve/reject result
  Profiles -> profile card -> access / status / identity / deletion
  Servers -> server card -> installation / settings / maintenance
  Status -> affected servers and operation details
  Announcements -> compose / preview / delivery
  Settings -> access policy / SSH key / traffic / alerts / updates / backups / cleanup
```

Keep existing destinations available throughout the migration. Accounts remain
part of profile management rather than a separate top-level destination.

## Phase 1: administration home and access requests

### Administration home

1. Heading: Administration (with the configured bot title as context).
2. A compact stored overview: enabled/total servers, active/total profiles,
   pending requests and servers needing attention.
3. When something needs attention, a visible Attention section with the count
   and its relevant Requests or Affected servers button. Omit this section
   when empty. An unavailable overview must not prevent navigation.
4. Management section: `[Profiles] [Servers]`.
5. Access management section and `[Requests]` only when there are pending requests.
6. System section: `[Status] [Settings]`, followed by `[Announcements]`.
7. One divider and the Back link to the member menu.

Do not expose the full status report on the home screen. Keep the current
Status destination for the detailed overview; a lightweight count on home
does not replace it.

### Requests list

1. Heading and total pending count if the API supplies it; otherwise describe
   the current page without pretending its length is the global total.
2. `[Search]` and, only during a search, `[Clear]` in one row.
3. Each request: readable username/name, short request context, then `[Review]`.
   Put technical account/request identifiers in a collapsed Details block.
4. Dividers between requests, compact pagination beneath the list.
5. One divider and Back to Administration.

Do not put destructive one-tap Reject buttons throughout a dense list. The
existing admin notification may keep its direct decision actions, while the
list leads to an explicit review screen.

### Request review

- Visible: applicant identity, request state and time when available.
- Collapsed Details: Telegram ID, internal identifiers and other technical data.
- `[Approve] [Reject]` beside the decision context, then divider and Back.
- Preserve existing decision semantics and duplicate-decision handling.
- After a decision, show the remaining requests; return to Administration when
  no requests remain. The member's existing message becomes the decision notice
  with To menu, as implemented in the member baseline.

## Phase 2: profiles and access management

### Profiles list

- Top controls: `[Add profile] [Search]`; show Clear only while filtering.
- One compact clickable row per profile: `display name · status`. No per-profile
  heading, separate status paragraph or redundant Open row.
- Do not fetch grants or live agent state individually for every list item.
- Preserve search and position when returning from a card; arrow pagination
  and one final Back divider.

### Profile card

1. Heading: profile display name (H1).
2. Visible summary: status, permanent or finite duration, and count of granted servers.
3. Primary row: `[Access] [Edit]`, followed by a quiet Management entry.
4. Access opens the regional grant editor; Edit contains name, expiry and
   Active/Frozen controls. No editing controls or server tables on the landing card.
5. Management contains account permissions, confirmed deletion, and Technical
   details. Operations are reached from Technical details, one level deeper.
6. Show only a short actionable warning for blocked synchronization; do not
   equate desired state with verified removal of access on an unreachable node.
7. One divider before Back, preserving the Profiles search/page context.
8. Administrator traffic display remains pending a dedicated consent-gated read.

### Manage access

- Show server sections grouped by region, including configured flags.
- Put available protocol buttons directly below each server; selected grants
  use primary styling. Preserve existing save semantics and revision checks.
- Keep draft choices across pagination. Show a visible Unsaved changes summary
  and one Save action if this flow uses a draft; do not mix immediate writes
  and draft changes without an explicit explanation.
- On save, report synchronization separately from saved desired access.
- Back returns to the profile card with the correct search/page context intact.
- Put Grant all / Revoke all above the server list, and Grant region / Revoke
  region directly below each region heading. Both operations update only the
  draft and require Save before affecting the nodes.
- Grant is additive and idempotent. It selects every configured protocol on
  currently enabled servers in the chosen scope, including other pages, while
  preserving grants outside that scope. Revoke removes all grants in the scope,
  including grants on disabled servers. Revoke all also clears stale grants
  whose server is no longer in the registry.
- These buttons are explicit snapshots, not persistent region subscriptions.
  A server added later receives access only after another explicit Grant press
  and Save. Resolve the current registry when the button is pressed rather than
  using only the displayed page. The same behavior applies during creation.
- Bind region callbacks to raw region names using short opaque tokens; never
  serialize long/Unicode names into callback data or let stale region indexes
  refer to a different region. Old tokens refresh the editor without changing
  grants. Creation buttons are also bound to their creation session.

### Profile creation

Retain the current wizard and all supported identity/access choices. Give each
step a short heading, required input and, if useful, collapsed Help. Back goes
one step back; Back and Next/Skip share a row. The final review keeps name,
identity and selected access visible, with generated/internal details collapsed.
Creation errors retain entered values; success opens the profile card.

## Phase 3: servers, installation and protocol settings

### Servers list

- `[Add server] [Search]` at the top, Clear only during search.
- Region headings; each server shows flag/name, a concise state and `[Open]`.
- Show Not installed, Changes pending, Applying and Needs attention explicitly.
  An unreachable agent is distinct from a newly created server without an agent.
- Use stored summaries rather than probing every server while listing them.
- Pagination and one final Back divider; preserve position on return.

### Server card

1. Heading: flag and server name; region/address below it.
2. Visible overall state and recommended next action:
   - Agent absent: Set up agent.
   - Agent ready, protocols absent: Install protocols.
   - Pending configuration changes: Apply changes.
   - Running operation: View operation.
   - Unreachable or blocked: clear reason and the existing supported recovery path.
3. Agent section: reachability, binary version when known, `[Probe]` and agent
   setup/update action when appropriate. Show runtime package version separately;
   do not invent an agent version from the protocol runtime manifest.
4. Services section: AWG and VLESS state, with the corresponding Settings buttons
   placed beside their service context. Details such as ports/transport live in
   collapsed service details unless needed for an error.
5. Configuration section: Applied or Changes pending, `[Settings]`; put the single
   Apply changes action on the main settings screen when a change is pending.
6. Access section: ready/pending/failed counts and relevant operation link.
7. Collapsed Details: node key, SSH/local target, revisions, manifest/path facts,
   notes and observation timestamps when supplied by the API.
8. `[Maintenance]`, then divider and Back to Servers. Keep deletion inside the
   maintenance flow, rather than adding another independent cleanup entry.

### Server settings

- General, Connection, Protocols, AmneziaWG and VLESS sections contain short
  summaries with their own Edit/Open buttons.
- Show unapplied changes prominently above one Apply changes button. Do not
  duplicate this button in protocol submenus.
- Protocol selections and VLESS transport choices are compact highlighted rows.
- Parameter pages show useful current values; generated entropy, keys and long
  configuration material are collapsed or explicitly retrieved as appropriate.
- Preserve generated-default behavior, validation, rotation/entropy confirmation
  and desired/applied revision semantics.

### Installation and operation results

- Visible: queued/running/completed/needs attention and the current known step.
- State exactly what finished: Docker, agent, runtime or protocols. No raw shell
  output as the main success message and no fake progress percentages.
- Collapsed Details for bounded diagnostics; explicit download for larger logs
  after sanitization. Hidden errors still need a readable visible summary.
- Place the next relevant action beside the result. Leaving the screen must not
  cancel an existing durable backend job or replay it on return.
- Retain the Docker prerequisite flow and existing confirmations. Apply the
  same rules to local and SSH installations.

Server creation keeps the current wizard steps and generated settings; it uses
the same Back/Next/Skip conventions as profile creation.

## Phase 4: remaining destinations

Only start after the first three phases have passed focused acceptance checks.

| Area | Rich Message treatment |
| --- | --- |
| Status and affected servers | Compact visible counts; grouped affected-server sections with Open buttons; diagnostic facts collapsed; explicit Refresh. |
| Announcements | Visible audience and delivery options beside their selectors; preview before Send; delivery counts visible and failed-recipient details collapsed. |
| Global settings | Sections for access policy, traffic collection and notification policies; selected options highlighted, consequences visible. |
| SSH key | Visible readiness and fingerprint; copy/download actions beside public key; no automatic private-key exposure. |
| Updates | Installed/available versions visible; update actions only when needed; rollout issues visible and per-node details collapsed. |
| Backups | List with date/type/status and matching download/restore action; details collapsed; separate restore confirmation. |
| Alerts | Visible active conditions with affected-server links; detailed observations and policy explanations collapsed. |
| Maintenance and cleanup | Clear scope and explicit confirmations; execution/result visible, diagnostics collapsed; unavailable-agent registry-only removal stays a separately explained choice. |
| Factory reset/full stack removal | Preserve typed confirmations, backend job verification and delivery-before-shutdown behavior; redesign presentation without weakening ownership checks. |

## Table placement

| Screen | Suggested columns | Visibility |
| --- | --- | --- |
| Administration home / Status | Item, Count or State | Visible compact overview; actions below relevant sections. |
| Profile card | Server, Protocols, Access state | Collapsed when long; paginated; readable names with flags. |
| Server card: agent/runtime | Component, Version, State | Visible short table; distinguish agent binary from runtime package. |
| Server card: services | Service, State, Port | Visible short table; transport/path details collapsed. |
| Pending settings review | Parameter, Applied, Pending | Visible before Apply; only changed values, with long values in details. |
| Update overview | Component, Installed, Available | Visible; rollout buttons below, never embedded in cells. |
| Operation result | Step or Server, Result | Visible summary; error explanations and logs in separate blocks. |

Tables complement sections rather than replacing every list. Profile/request
lists need actions next to individual records, so use record sections there.
Do not synthesize unknown applied values from desired settings: a comparison
table requires an actual applied snapshot from the backend or an explicit Unknown.

## Implementation checkpoints

For each phase:

1. Inventory the existing handlers, buttons, error states and backend calls;
   map each current action to its new location before changing presentation.
2. Implement only that phase, preserving all existing functionality and i18n.
3. Test Rich block/table structure, cell headers and fallback row labels,
   button callbacks/styles, exactly one Back divider,
   stale callbacks, fallback delivery, missing data and backend failure states.
4. Validate real Telegram mobile layout before expanding the redesign further.
   In particular check long names, Russian labels, embedded buttons inside
   collapsed sections, edits after paging, and return navigation.
5. Record completion and any backend data gaps here. Do not claim live layout
   acceptance based only on serialization tests.

### Phase 1 implementation record

- Administration home uses an embedded overview table, conditional attention
  actions and Management, Requests and System sections. Optional summary reads
  are concurrent and bounded; missing summary data does not hide navigation.
- Request list, review, search, error and notification screens use embedded
  buttons. Identifiers are collapsed, decisions use primary/destructive styles,
  and Back is separated once from the rest of the screen.
- Search Back preserves the prior filter/page and exits the input state.
- Native compact tables preserve labeled rows in the plain-text fallback.
- Existing access decision handling and requester single-message navigation
  are retained. No fleet probes or new backend permissions were introduced.
- Live Telegram mobile acceptance is still pending; automated tests validate
  structure, callbacks, localization, errors and fallback behavior.

### Phase 2 implementation record

- Profile list, card, identity forms, status, access, deletion confirmation and
  creation wizard now use embedded Rich actions and English/Russian text.
- The landing card exposes Access/Edit with Management below. Identity, expiry
  and status controls live in Edit; IDs and operation details live in
  Management → Technical details. Operations use a node/protocol/result table.
- Access tables and editors load every backend server cursor page, sort by
  region/name, and display 10 servers per UI page. No 20-grant truncation.
- Grant/revoke all and regional buttons work in both creation and editing, with
  explicit-save semantics. Page transitions retain the draft; leaving the
  editor for the card discards it.
- Save uses the revision captured at the beginning of editing. A revision
  conflict preserves the draft for review and does not automatically replay it.
- Active/Frozen choices set an explicit value; pressing the already selected
  value does not toggle it. Deleting profiles expose no mutation actions.
- Back/Next share a row with a single divider; primary Next/Create styling is
  preserved when Back is rendered as a navigation link.
- Administrator traffic display remains deferred: the existing member summary
  endpoint deliberately permits only the owner. Do not bypass that check or
  the global collection/member consent gates to fill an administrator table.
- Live Telegram layout and real provisioning acceptance remain pending.

Next remaining Phase 2 item is the gated administrator traffic read/display.
Core server screens are implemented as described in the follow-up below.

### Review fixes after Phase 1 testing

- Deleting a member's final VPN profile also changes their account approval to
  pending in the same transaction. Administrators and owners of another live
  profile retain their status. Startup repairs legacy approvals left behind by
  earlier deletion commands; replaying a deletion does not repeat account changes.
- The Telegram adapter clears the revoked member's cached home presentation and
  replaces their existing control message with the access gate when delivery is
  available. Backend authorization enforces the revocation independently.
- Reapproval creates a fresh default profile when only deletion tombstones remain,
  with a new runtime identity and no inherited grants or configurations.
- Decorative emoji are removed from localized labels. Server flags remain;
  selected options use button color rather than checkmark prefixes.
- Admin home uses `Admin panel · Bot name` and
  `Access management · Pending: count` headings, without the stored-state caption.
- Request search appears only above five pending requests. The API exposes the
  global pending count so pagination and filtering do not hide it incorrectly;
  an existing search can always be cleared.

- Language changes redraw settings and highlight the selected language without
  adding a confirmation line.

### Profile card simplification (implemented)

Keep the landing card focused on the selected person: display name, profile
status, expiry when set, and a compact count of accessible servers. Avoid inline
editing controls and routine synchronization details on that screen.

- **Access** opens the existing regional grant editor, including explicit grant
  and revoke actions for all servers or a region. The full server list belongs
  here rather than on the landing card.
- **Edit** contains display name, expiry and profile freeze/unfreeze controls.
  Each action returns to this screen, with Back returning to the profile card.
- **Management** contains account permissions and profile deletion. Both are
  sensitive actions with explicit, target-specific confirmation screens.
- **Technical details**, reached from Management, contains identifiers and
  revisions. The latest operation and per-node task details are one level deeper.
- Surface a short actionable warning on the landing card only when recovery is
  needed. Routine successful synchronization does not require its own section.

Use one primary action row for Access and Edit, a quiet Management entry, then
one divider before Back. This accepted redesign is implemented.

### Notification decisions and administrator promotion

Implemented before and retained in the accepted card redesign:

- Approving or rejecting from a standalone notification replaces it with the decision
  and Close; approval also offers Edit profile. This preserves the administrator's current control screen, wizard and list
  filters. Reviewing a notification retains notification-specific decision
  callbacks and does not replace the saved control-message identity. Stale
  notifications are removed; transient backend errors do not discard the request.
- Profile Management exposes Account permissions for owned, non-deleting
  profiles. An approved member can be made an administrator from a separate
  confirmation screen naming the exact account and warning about full node,
  access, settings and destructive-operation permissions.
- The confirmation is bound to the adapter user's FSM, target profile/account,
  message, nonce, captured account revision and idempotency key. Going Back
  discards it. Backend authorization and revision checks remain authoritative;
  confirmations are never rebased automatically after an account change.
- After promotion, the member's cached home presentation is invalidated so the
  next home navigation reflects the new administrator role. Existing backend
  protections against self-revocation and removal of the last administrator
  continue to apply to account role mutations.

### Profile list density and heading hierarchy (implemented)

The user accepted this layout together with the profile-card redesign.

- Use a single compact profile row: `username · status`, rather than a separate
  heading and status line. Keep the profile-open action adjacent to its row.
- Reserve Heading 1 for the screen title, Heading 2 for sections/regions, and
  Heading 3 for nested server groups where a heading is useful. Profile rows
  can use ordinary text instead of adding another heading level.
- The hierarchy is verified against the installed Rich Message schema and
  rendering tests. Telegram mobile appearance still requires a live UX check.

The installed aiogram schema confirms heading sizes 1 through 6 (1 is largest).
Screen titles now use size 1, sections use size 2, and nested server headings
use size 3. Collapsed groups do not introduce an invisible extra heading level;
compact profile rows have no headings.

### Expiry editing and scheduled runtime revocation

- Edit offers 7/30/90-day intervals, no expiry, or a specific YYYY-MM-DD date.
  Custom dates end at 23:59:59 UTC on the chosen day. Past/invalid dates are
  rejected without a mutation. Forms preserve their starting profile revision
  and idempotency key; conflicts require returning to refresh the form.
- The backend worker now detects reached expiries that still have an ensure
  intent at the current revision. Under maintenance admission and a revision
  guard, it records a fresh durable delete revision using the existing outbox.
  This does not require Telegram activity and does not repeat on each timer run.
- Audit attribution preserves the account that requested the expiring revision.
  Grants and identities remain stored, allowing a later extension to restore
  access. Remote calls remain outside DB transactions. Existing blocked-node
  protections still apply: uncertain earlier work is not replayed automatically.
- Revocation is processed on the worker timer, subject to agent availability;
  the configuration API separately denies issuance as soon as expiry is reached.


### Request visibility and isolated profile setup (implemented)

- The administrator home shows the Requests entry and Access management section
  only when the overview confirms at least one pending request.
- Approve/reject edits the notification in place. Rejection offers Close;
  approval offers Edit profile and Close. Close deletes this notification.
- Edit profile opens a separate grants → duration → review wizard in the same
  notification. After saving, the overview offers Edit and Close. Add user from
  Profiles uses the same grants and duration setup for the automatic profile.
- Each notification uses its own FSM storage destiny keyed by message ID.
  The main panel's message identity, active wizard, filters and draft are retained.
  Date input in a notification must be a reply to that message; unthreaded input
  continues to belong to the main panel. Closed sessions reject stale callbacks.
- A profile PATCH can atomically replace grants and expiry, with the captured
  revision and an idempotency key. Validation failures do not partially save.
- Member access may be permanent or expire after 7/30/90 days or on a custom
  date. Administrators have permanent duration: finite expiry is rejected by the
  backend, promotion clears existing expiry with a fresh runtime intent, and the
  worker repairs old finite administrator expiries. Grants and freezing remain
  independent controls; permanent duration does not grant access to every node.
- Navigation arrows use Unicode text symbols rather than emoji.


### Member Profile presentation follow-up (implemented after alpha.30)

- Keep the profile name directly below the title. Access contains visible
  status and duration; Account contains Telegram identity and creation date.
- Render summary statistics in a compact two-column native table, including
  server/connection counts, protocol counts and config issuance activity.
- Traffic is a separate visible section with the current UTC month total and
  protocol table, only when the existing backend collection/consent gates allow
  it. No new queries or permissions are introduced.
- When global collection is enabled but member consent is missing, show a short
  hint inside Statistics about enabling it in Settings. Hide that hint entirely
  when the administrator has disabled collection globally.
- The final Servers block remains collapsed, grouped by region with flags and
  ten-server pagination. Paging retains the summary above it. Server traffic
  uses compact protocol/usage tables; missing or stale samples remain explicit.
- Member server lists remain informational, without config-selection actions.
  Exactly one divider separates Back from the screen content. RU/EN and labeled
  plain-message fallbacks are retained.

### Server navigation and settings follow-up (implemented after alpha.30)

- The landing card prioritizes installation, explicit Probe, services and pending
  configuration. Opening it reads stored state without implicitly contacting an
  agent. Installation remains reachable when settings are pending; active or
  blocked operations expose a direct recovery entry.
- Management contains Maintenance and Technical details. Diagnostics, runtime
  and recovery tools sit behind Technical details rather than on the landing
  card or the main settings screen. Full removal retains its maintenance path.
- Settings separates General, Connection and protocol configuration. Compact
  tables accompany related edit actions; advanced options are collapsed. Private
  keys and entropy are not implicitly fetched into hidden blocks.
- Apply changes appears only on the main Settings screen and only when there
  are unapplied revisions. Existing field editors and confirmation flows remain
  available, with Back returning to their owning section.
- Server lists use backend region/title/key sorting with scoped composite cursors
  so grouping remains consistent across pages. Search and page context survive
  navigation; lists do not probe the fleet.
- Creation offers two-column region presets with globe prefixes: Europe/Africa
  use 🌍, Asia/Oceania use 🌏, and North/South America use 🌎. Other accepts a
  custom region. Presets store canonical region names independently of the node
  flag, and Back preserves the wizard draft.
- Server screens use embedded Rich controls and navigation dividers with RU/EN
  text and plain-message fallback. Detailed diagnostic/result presentation and
  live mobile acceptance remain follow-up work.
- Validation: 725 automated tests pass, including region cursor pagination,
  retained field actions, conditional Apply and region wizard navigation.

### Wizard and diagnostics refinement (after alpha.31)

- SSH/Local and protocol choices share compact rows. Existing selections use
  primary color, including when returning to an earlier wizard step.
- The creation review separates presentation, connection and services into
  sections, with compact facts tables. Internal key and SSH target move into
  collapsed technical details. Back/Create retains the existing draft and
  explicit creation semantics.
- Diagnostics uses a labeled service-facts table. Successful port checks use
  port/protocol/result columns; unmanaged firewall warnings remain visible.
  Operation identifiers are available in collapsed technical details, while
  refresh and recovery actions remain next to the visible outcome.
- Validation: 727 automated tests pass. Further installation, Probe, rollout
  and removal presentation refinements and mobile acceptance remain pending.

### Installation, Probe and removal refinement

- Installation identifies the selected server and places its current prerequisite
  or installation choice beside the Next step section. Existing Docker checks,
  reusable-config validation and settings requirements remain authoritative.
- Probe keeps agent state and config presence visible. Versions and explanatory
  notes are collapsed; diagnostics separates service facts from technical context.
  Missing configs are not described as running services.
- Rollout progress explains that execution continues independently of navigation.
  Task identifiers are collapsed; refresh, settings and Rust recovery actions
  retain their existing status-specific availability.
- Maintenance returns to Management. Verification targets and background safety
  notes are collapsed, while cleanup state and pending/blocked work stay visible.
  Full and registry-only removal confirmations use destructive button styling;
  the remote-state uncertainty warning remains visible.
- Removal progress groups refresh/retry and registry-only recovery in Next step.
  No remote execution, authorization or deletion semantics have changed.
- Validation: the full 727-test suite passes, plus two additional RU/EN rollout
  and unreachable-removal presentation checks. Live Telegram acceptance remains
  necessary; generic edit/input screens and deeper runtime/recovery tables can
  receive further polish after feedback.

### Edit forms, runtime details and SSH installation audit

- Local/SSH choices share one row and highlight the current connection.
  Connection facts use a native table; explanatory notes are collapsed. Back
  returns to the Connection section rather than skipping directly to Settings.
- Value editors separate the current value from the input prompt and collapse
  application guidance. AWG preset choices use compact rows and selected styling.
- Runtime shows installed/target versions in a table, with commit identifiers
  collapsed. Recovery actions remain available; clean reinstall and runtime
  cleanup confirmations use destructive styling without hiding warnings.
- Remote agent installation no longer requires the sudo executable when logged
  in as root. Other users use noninteractive sudo, with an explicit prerequisites
  check before remote deployment. Bash streaming and command quoting are retained.
- SSH/SCP use a 15-second connection timeout and keepalive failure detection.
  Explicit identity files use IdentitiesOnly to avoid unrelated agent keys.
  Prerequisite failures have a localized rollout recovery message rather than
  exposing raw console output.
- Validation: 733 tests pass, including simulated root/non-root remote privilege
  branches, nested streamed scripts, quoting, SSH connection options and the
  backend failure-code mapping. Shell syntax checks pass. No live SSH host was
  contacted; cold-host installation still requires the user's acceptance test.

### Phase 4: Status and affected nodes (implemented after alpha.32)

- Status uses a compact counts table for servers, profiles, frozen profiles,
  pending requests and affected nodes. Requests and affected-node actions appear
  only when relevant; management links and explicit Refresh remain available.
- Version and the stored-state/live-runtime explanation are in collapsed
  technical details. The screen reads the authorized overview without agent RPCs.
- The backend overview now includes each affected node's region in its typed
  response. The affected list groups nodes by region, prefixes configured flags
  and shows ten items per page with arrow navigation only when needed.
- Refresh reevaluates current affected nodes and clamps the page after recovery,
  including a proper empty state. Node cards remain accessible from each row.
- Both screens use embedded controls, the shared heading hierarchy, RU/EN text
  and one final Back divider. Announcements and the remaining Phase 4 settings
  destinations are the next independent blocks.

### Server UX corrections after alpha.32 testing

These changes supersede the earlier multiple protocol-entry paths and the
main-settings-only Apply rule.

- The landing card uses `region · flag/name`. A single existing Advanced & Runtime
  entry is highlighted when edits are pending; no duplicate settings entry is
  inserted above it. Protocol editing is reached through this settings hub only.
- The hub groups protocol-name buttons under Protocol settings. Its screen title
  and ordinary settings/back labels no longer reuse Advanced & Runtime.
- Edits use one revision-bound adapter draft across settings subpages. Protocol
  and transport selections use colored buttons and remain selected when returning
  from another screen. At least one protocol and one VLESS transport must remain.
- Save and apply and Reset changes appear beside the edited settings, including
  the protocol selection screen. Save sends only changed fields with the captured
  revision and idempotency key before queuing explicit application. Reset discards
  the adapter draft and reloads backend values in the current settings section;
  it does not roll back previously committed changes or an executing operation.
- Backend rejection of protocol removal with existing grants is explained and
  preserves the draft. Revision conflicts likewise preserve edits for review,
  without silently rebasing them on concurrent changes.
- Bootstrap appears on the landing card for an uninstalled node. Once installed,
  reinstall is reached through Management. Existing configuration validation
  still determines whether preserving configs is offered.
- Region editing uses the same globe-prefixed templates and custom Other option
  as creation, independently of the node flag.
