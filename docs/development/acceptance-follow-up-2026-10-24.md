# Acceptance follow-up — 2026-10-24

Status: Batch 36 implementation and local automated verification completed on 2026-10-25.
Owner visual/live verification remains pending. The original reported requirements below are retained as the acceptance checklist.

## Priority: blocked operations

Audit every durable operation that can become blocked/unconfirmed, including
operations outside the central recovery inventory. For each, trace the backend
state machine, available evidence, maintenance gates, bot and Workstation paths,
and terminal propagation to parent operations. Record safe resume/reconcile/cancel
or an explicit limitation. Never clear fences or replay unknown mutations blindly.
See `operation-recovery-audit.md` for the separate source audit when available.

Before installation/agent readiness, a node must expose only settings, setup and
registry removal. Hide and reject probe/diagnostics/open ports/repair and other
runtime operations, including stale callbacks and direct API requests. The owner
observed a blocked operation before installing the agent; inspect the lifecycle
predicate rather than treating every unreachable installed agent as uninstalled.

## SSH and server creation

- First-login SSH authentication chooser: use the temporary-config option style,
  always-white focus arrow, activate the selected option directly; remove
  Cancel/Continue buttons from this chooser.
- After choosing “On another VPS via SSH”, before templates, show SSH preparation
  only when no SSH server exists (a local server does not suppress it). Explain
  adding the bot SSH public key; initially collapse both guide and public key;
  offer sending the key separately for iOS. Bottom row: Back left, colored
  Continue right. Guide/key controls remain available above this row.
- Protocol picker: replace hardcoded “VLESS will enable TCP and XHTTP by default”
  with actual installation-default transports, e.g. `VLESS — TCP · XHTTP`.
- Server/agent setup button belongs only on the root node screen.
- Successful Server setup screen: only Close; remove Refresh status and Back.
- Probe: Disk usage, used percentage and used / total capacity, not Disk free.
- Public-key screen: explicit “Bot public key” heading between fingerprint and
  actual `ssh-ed25519 ...` key.
- Technical details → Diagnostics must include Diagnostics in breadcrumbs.

## Quick Start

- Remove obsolete numbered Quick Start placeholder from bot welcome message.
- Audit every guide action label: describe the action/destination, e.g.
  Configure installation defaults, Add server, Get config. Continue only for
  actual next-step transitions; retain existing explicit completion semantics.

## Accounts, requests and expiry

- Remove unused footer space under administrator-profile buttons in NPW.
- Request list entries are buttons labeled `@username/tg_id · DD/MM/YY`, preferring
  username and falling back to Telegram ID.
- Admin request notification: put full review information on Approve/Reject
  screen; remove separate Review request button.
- Rename Specific date to Custom. Bot and NPW both accept `YYYY-MM-DD` and combined
  durations such as `3d5m1y` (years/months/days); update hints consistently.
  Define validation/calendar behavior explicitly; preserve authorization and
  revision checks. Do not silently interpret months as minutes.
- After delete account in NPW, no stale Unblock button/card may remain.
- Requests disabled: hide request notification settings, expose Stub message.
  Requests enabled: expose notification settings and Welcome message instead.
  Use neutral default welcome wording without mentioning VPN. Mirror semantics
  and settings in NPW; determine whether separate stub/welcome storage is needed.

## Issuance and temporary configurations

- AWG waiting text: “Проверяем доступ и состояние ноды…” like VLESS, rather than
  claiming device creation every time a one-device account requests config.
- Ordinary VLESS configuration names include transport, not only server/VLESS.
- Shorten temporary-creation breadcrumbs so the bot does not truncate the useful
  path. Keep selected parameters on one line, e.g. server · protocol · duration.
- Send link separately (iOS): monospace/copyable, not plain text.
- Temporary client import names include Temp, e.g. `AWG · Finland #1 · Temp`;
  preserve protocol/transport distinctions as relevant.
- VLESS connection-expiry caveat: show only at create confirmation and config
  information card, preceded by a blank line. Remove from creation progress and
  artifact screen (QR/link/files).
- Artifact screen breadcrumbs include config identity and Config, e.g.
  Admin › System settings › Temporary configurations › Finland #1 · VLESS · TCP
  › Config. Back returns to that config's information card, not the whole list.
- Artifact screen matches ordinary user config presentation; no Refresh/Revoke
  actions there. Keep those actions on the config information card.

## Alerts, traffic and consistency

- Remove CPU sampling/check/average implementation text from alerts.
- Monitoring off: hide all alert settings, Active alerts, Policy and delivery
  details. Investigate reliable automatic updates while the screen stays open.
  Remove Refresh status only if a real bounded background refresh mechanism
  supports this; otherwise retain it as the owner explicitly allowed.
- Traffic collection off: hide Collection status section too.
- Rename all bot Check again buttons to Refresh.

## Acceptance boundaries

- Owner completed near-full Telegram UI walk; new findings above need focused
  retest, not a repeat of every previous scenario.
- Real-client IPv6 leak test remains OPEN: home ISP has no IPv6, mobile test is
  postponed. Do not count IPv4-only testing as an IPv6 leak test.
- Existing node VPN serves another person. Do not touch owner runtime or dispatch
  cleanup/setup while investigating. Tests use isolated data and fake drivers.
- No release requested for this batch yet.

## Initial audit verification

On 2026-10-24, existing `test_backend_recovery.py` and
`test_telegram_recovery.py`: 29 PASS in the existing isolated Python 3.12
environment. First sandbox run was interrupted after a TestClient hang; the
bounded rerun outside sandbox completed in 3.14s. One existing Starlette/httpx
deprecation warning; no checks suppressed. These are existing regression tests,
not proof that every blocked state is recoverable.

Two extra disposable in-memory database probes, without driver calls:

- A registered node with `enabled=0, applied_revision=0` accepts `open_ports`
  as `awaiting_executor`. Confirmed admission gap in `NodeOperations.queue`;
  no production operation was dispatched.
- Recovery inventory offers no actions for blocked `version`, `agents`,
  `runtime` updates, or blocked stack core lacking a unit name. This confirms
  a missing offered path, not permission to discard uncertain execution evidence.
  Separate operator-repair paths and actual reachability of each state must be
  assessed in the full audit.

A third probe exercised the real update state machine, not a manually injected
blocked record: queue `agents` with the test fixture's updater/driver and a pure
`runner=lambda args: False`; advance worker to blocked driver item and blocked
parent. Inventory returns `actions=[]`. Replacing runner with success and
calling worker returns False; the parent remains blocked. No installer process
or remote operation is invoked. This is a reachable recovery gap even after the
underlying execution problem is fixed.

---

## Batch 36 Implementation & Verification Status (2026-10-25)

### Implemented & Locally Verified in Focused Suites

- **Update Gate Invariants**: `backend_controller_update_gate` created for both stack and version updates; predispatch cancellation cleanly drops gate; uncertain launch/receipt retains gate and rejects subsequent queues (`update_pending` / HTTP 409). Negative evidence tests verified without manual gate-clearing workarounds.
- **SSH Preparation Screen**: Appears between SSH choice and templates only when `ssh_preparation_required` is true (local node does not suppress; any SSH node clears it). Collapsed guide/key, iOS link, Back left and Continue right.
- **Node Templates & Protocols**: VLESS transports inherited from defaults (`TCP` vs `TCP · XHTTP`); AWG-only omits VLESS hint.
- **Uninstalled Node Draft Controls**: Returns empty in submenus for `applied_revision == 0`; setup button appears on root screen only.
- **Probe Disk Usage**: Formatted as `100 - free%` and `{used} / {total}` bytes (e.g. `75% · 75.0 GiB / 100.0 GiB`).
- **Temporary Configs UI & Artifact Separation**: Artifact view restricted to Back to card, no Refresh/Revoke/caveat. VLESS notice shown on confirmation and active card with leading newline; suppressed on queued/issuing. One-line selections on confirmation. iOS link code entity with UTF-16 length and close panel.
- **Config Issuance & Temp Naming**: VLESS files and fragments include `TCP`/`XHTTP`; temporary configs include `Temp`; AWG refresh includes `AWG · {server} · Temp`.
- **Request Setup & Notification**: Admin requests compact `@username/tg_id · DD/MM/YY`; admin notice includes full review details directly without Review request button.
- **Navigation & Screen.rich**: `Bot public key` heading before noncollapsed code even with `uri_rows`; normal client URI screens stay collapsed; `node_tools` path breadcrumbs retain `Technical details` parent and `Diagnostics` current breadcrumb.
- **Parser Parity**: Python rejects duration internal spaces (`'1d 2h'`); Rust rejects signed or malformed ISO date components (`2099-+1-01`, `2099-01-+1`).
- **NPW Access Screen Footer**: Pinned Back button verified outside scrollable footer bounds with member card present; request tab compact labels verified with `_account` fallback.

### Owner Live Verification

- **Owner Live Visual Walk**: Owner visual inspection of Telegram UI and NPW TUI.
- **IPv6 Acceptance**: Real-client IPv6 leak test remains OPEN (owner ISP lacks IPv6; mobile test postponed).
- **Local automated gate**: 1468 Python tests passed (149 integration-fixture skips, 2 existing warnings); 334 Rust tests passed; 12 PostgreSQL recovery tests passed separately. Strict Clippy, formatting, shell syntax, diff whitespace and fresh NPW --no-tui version/help checks passed. Disposable PostgreSQL stopped with data retained.

### Earlier Focused Test Execution (2026-10-25)

- **Python (dev312 pytest)**:
  - 159 passed across 11 allowed test suites in 13.44s.
- **Rust (toolchain 1.90.0 cargo test --locked --offline)**:
  - 3 focused tests passed in `rust/node-plane-cli/src/tui/tests.rs` (`access_request_tab_labels_use_compact_identity_and_date_with_account_fallback`, `custom_duration_accepts_units_and_rejects_invalid_or_overflowing_values`, `access_screen_admin_profile_footer_gap_and_deleting_profile_actions`).

## Deferred keyboard ergonomics

Owner explicitly deferred NPW word deletion on 2026-10-25: add Ctrl+W and Ctrl+Backspace consistently across text inputs in a later batch. Ctrl+Backspace may be indistinguishable from Backspace in some terminals; preserve ordinary Backspace behavior. No implementation belongs to the current batch.
