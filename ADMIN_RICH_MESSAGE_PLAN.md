# Administrator Rich Message UI plan

Status: Phase 1 published in `v0.4.3-alpha.28`; live acceptance is in progress.
Phase 2 profile, access and creation screens are implemented after that tag.
The administrator traffic summary described below still needs a dedicated
authorized backend read; remaining phases are planned.

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
5. Requests section: pending count or a short empty state, then `[Requests]`.
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
- One compact section per profile: display name, Active/Frozen/Deleting and a
  short synchronization issue if present, followed by `[Open]`.
- Do not fetch grants or live agent state individually for every list item.
- Preserve search and position when returning from a card; arrow pagination
  and one final Back divider.

### Profile card

1. Heading: profile display name.
2. Visible summary: status and access synchronization result. Pending or blocked
   synchronization stays visible and links to its operation details.
3. Access section: concise server/protocol summary and `[Manage access]`.
   A long server list is collapsed and paginated. No silent truncation at 20
   grants and no missing names caused by loading only the first server page.
4. Status section: `[Active] [Frozen]`, selected state highlighted. Explain
   synchronization failures beside these controls; do not equate desired state
   with confirmed removal of access on an unreachable node.
5. Identity section: `[Rename]`; collapsed Details contains profile/account IDs,
   Telegram binding and revision data. Profiles without Telegram remain valid.
6. Traffic summary only under the existing admin collection and member consent
   gates. Do not introduce additional collection as part of a UI redesign.
7. Collapsed Management section: deletion entry and other uncommon existing
   actions. Deletion opens a separate confirmation screen.
8. `[Refresh]`, then divider and Back to Profiles.

A separate generic Edit menu should become unnecessary for these frequent
actions. Preserve its callbacks or route them to the new destinations during
the migration so old screens remain navigable.

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
- The card exposes common actions directly; old Edit callbacks open the card.
  IDs are collapsed, desired status and synchronization remain visible, and
  operation details use a node/protocol/result table.
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
Server screens form the next independent presentation block.

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
