# Changelog

## Unreleased

## 1.0.0-beta.2 - 2026-10-26

- Preserve the configured Telegram adapter credential across controller reset, including resets started from Workstation; continue invalidating unrelated credentials.
- Fix HTTP 500 responses when installation progress includes skipped stages, and validate node keys and edited fields before advancing through forms. Explain reserved keys and suggest a free numeric suffix.
- Reject duplicate Workstation installation names, support Ctrl+W/Ctrl+Backspace in TUI inputs, correct post-creation navigation, simplify uninstalled-node removal and regroup Quick Start and IPv6 defaults.
- Release the local installation catalog lock after rejected edits as well as successful saves, including when a concurrently spawned child inherited the descriptor.

## 1.0.0-beta.1 - 2026-10-26

- Support numbered alpha, beta and rc releases in controller installation, update catalogs, Workstation bootstrap installers and release tooling; compare prerelease numbers correctly and keep development and stable channels separate.
- This beta requires a fresh installation when coming from alpha.20: that controller's release selector only recognizes alpha tags. Later beta/rc updates use the normal development-channel workflow.
- Ship LICENSE and generated third-party Rust dependency notices in Workstation, Driver, Agent and controller archives; check notice freshness before publication.
- Remove obsolete Xray runtime helpers, unused Python namespaces and validation helpers, unused Rust dependencies and the unused pytest development stack; retain locked dependency versions and compatibility exports.
- Label removal of an uninstalled node simply Remove; clarify that a failed final Telegram Bot API check can be temporary and point to Diagnostics → Installation diagnostics before reinstalling.

## 1.0.0-alpha.20 - 2026-10-25

- Restore release CI by exercising the PostgreSQL grant-policy race through the supported server setup coordinator; keep the prohibition on runtime actions for uninstalled nodes.
- Includes the recovery, account lifecycle and interface fixes prepared for alpha.19. The schema 14 rollback requirement still applies.

## 1.0.0-alpha.19 - 2026-10-25

- Enforce controller update gate invariants to reject overlapping updates; support safe predispatch cancellation, blocked operation recovery (resume/recheck/cancel) across updates, backups, issuances, and cleanup jobs without unfenced mutation retries.
- Require Driver and Agent release binaries for setup and rollout; remove unsupported on-server Rust compilation paths.
- Align healthcheck inspections with installed releases and uv-managed runtimes, read environment files as structured data, and remove legacy portable deployment mode.
- Refine Telegram and Workstation interfaces: add an SSH preparation screen with collapsed guides and copyable iOS links, streamline Quick Start prompts and breadcrumbs, isolate temporary configuration artifact views from action controls, and accept ISO dates and composite custom durations (`3d5m1y`).
- Restrict account creation to administrator approval or manual provisioning, avoiding pre-allocation for visitor requests and preventing phantom recreation of deleted accounts.
- Schema migration 14 introduces visitor request tracking (minimum reader 14); rolling back to an older controller release requires restoring a prior database snapshot.

## 1.0.0-alpha.18 - 2026-10-18

- Manage Python dependencies with pyproject.toml and uv.lock; deploy exact runtime groups with locked sync, use the same lock in CI, and retain generated hashed requirements exports for published updaters.
- Treat confirmed access expiry, freeze/revocation and administratively disabled VPN as expected states in node summaries and diagnostics; preserve warnings for blocked or unconfirmed changes and real host faults.
- Keep pending requests and deleted accounts out of Workstation Profiles; show Blocked/Unblock correctly, refresh cached profile revisions after account changes, and remove duplicate approval summaries.
- Count successful AWG/VLESS issuance on any eligible server toward Telegram Quick Start; preserve explicit Done acknowledgement.
- Shorten ordinary and temporary configuration names to server · AWG/VLESS · device (when present).
- Retire missing Telegram decision notices so a deleted message cannot block updates for other administrators.
- Remove terminal QR rendering and its unused Rust dependencies; place Cancel before Close in the Workstation exit dialog.
- Return to Quick Start after guided server, protocol, access and configuration flows, while clearing that context when leaving for the main or admin menu.
- Allow keyboard selection of Cancel/Continue in Workstation password and text prompts, with matching button highlighting.
- Return guided installation-defaults editing to Telegram Quick Start, retain its breadcrumb context, and offer Continue/Edit installation defaults after returning.
- Remove Telegram breadcrumb dividers while preserving the divider before Back.
- Restore visible corner Back navigation in Workstation profile editing, access and duration menus; remove unused editor footer space and make Access keyboard navigation cycle and retain control indices.

## 1.0.0-alpha.17 - 2026-10-17

- Apply a newly selected second protocol through the existing managed settings command instead of repeatedly attempting fresh-node preparation.

- Group Workstation Operations by node or controller, with scoped task pages, refresh and Back navigation. Keep a single scrollable document within each page.
- Use contextual recovery labels such as Resume setup and Reapply settings; allow the agent preflight retry and setup cancellation routes in both clients.

## 1.0.0-alpha.16 - 2026-10-17

- Recover node setup after a confirmed SSH preflight failure, allow explicit cancellation before remote installation, and recover blocked protocol children without replaying an unfenced command.
- Verify Xray IPv6 settings only when Xray is selected, allowing AWG-only setup to finish normally.
- Offer an existing local SSH private key for the first Workstation connection, support encrypted keys and keyboard-interactive challenges, and check root/passwordless sudo before enrolling the Workstation key.
- Distinguish Telegram Bot API connectivity failures from rejected bot tokens during installation, bound the API check, and explain how to continue after an installed controller fails final verification.
- Scroll Operations text and actions together and clear completed skipped-stage text from the current installation progress.

## 1.0.0-alpha.15 - 2026-10-17

- Capture IPv6 in full-tunnel AWG configurations and preserve peer keys during dual-stack migration; add a shared IPv6 outbound switch for AWG/Xray and installation defaults.

- Document dev-channel installation on Linux/macOS and PowerShell, exact alpha tags, and source builds for the current dev branch or tags without published binaries; link source instructions from installer errors.
- Publish channel-aware workstation bootstrap assets and use tag-pinned dev/stable commands in release installation instructions.
- Wait for backend API readiness after confirmed service recovery, and distinguish a completed controller installation from incomplete workstation registration with a concrete continuation command.
- Accept an explicitly supplied Telegram administrator ID even when it matches the example value, and handle unsuffixed Agent/Driver archives without moving binaries onto themselves.
- Keep backup details concise with a Profiles label and short commit hash; show the server count without profile ratios in the admin Overview.
- Explain expired or revoked temporary configurations without fetching their artifacts again; refresh the card when revocation races artifact loading.

- Allow the first temporary configuration to start its expiry scheduler when new inactive systemd units cannot be reset; keep activation and readiness checks mandatory.
- Include temporary issuance and revocation failures in Operations, with targeted journal checks and cancellation only after the agent confirms absence and fences the original command.

- Preserve the installed controller when a stack snapshot fails, validate complete snapshots before restoring, and restore only explicitly recorded files and absences.
- Label account creation correctly in the Workstation audit while retaining the API endpoint in its details.
- Separate Telegram menu breadcrumbs from their content with a divider, and add a blank line before the first region in the profile access editor.
- Show a version mismatch in the Workstation Status line when the connected controller and NPW report different versions, without comparing commit hashes or using another installation's version.

## 1.0.0-alpha.14 - 2026-10-15

- Simplify Get config to a region-grouped server list followed by full protocol buttons. Skip the VLESS transport choice when only one transport is available and return AWG/device and transport screens to the selected server without reissuing configurations.
- Compact the Telegram Server access editor into one button per server with selected protocol labels. Put global and regional All/None actions and Persistent rules in text links, and edit individual protocols on a separate screen while retaining the draft and list page.
- Hide Profile details and traffic statistics after access expires; keep traffic totals only in their dedicated section and remove the incomplete-totals notice.
- Deliver access-expiry notifications from System through the existing announcement Inbox, using the configured service name and the same Close/Dismiss controls. Deduplicate each expiry and suppress queued notices after renewal.
- Use From for notification senders and show the sender and DD/MM/YY date in Inbox buttons. Mark only unread items as New, and show a labeled page counter only when there are multiple pages.

## 1.0.0-alpha.13 - 2026-10-15

- Add a custom theme wizard with named JSON files, primary/secondary color previews, an optional darker secondary color, and confirmed deletion of custom themes.
- Split Diagnostic into Installation diagnostics and Operations; show the Operations loading screen immediately. Preserve the latest navigation read while another request is running, bind settings actions to the current installation, and retain safeguards against stale responses and replayed mutations.
- Validate SSH user/host targets and public DNS/IPv4 addresses before advancing the node wizard, retaining the local controller-address shortcut. Use contextual Skip/Continue labels for optional flags and backup names.
- Keep dialog Back/Cancel on the left and primary actions on the right, filling the available row with only visible buttons. Keep Refresh before Create in temporary configurations and remove verbose CPU sampling policy text from Workstation alerts.
- Shorten the workstation theme documentation and point installer bootstrap links to the existing repository branch.

## 1.0.0-alpha.12 - 2026-10-14

- Sample CPU every 5 seconds, report a rolling minute average throughout node/controller diagnostics and check CPU alerts every 30 seconds independently of service checks and provisioning. Notify severity transitions and recovery with current measurements, explicit units and Close-only alert/recovery messages; preserve the separate alert Inbox.

- Add a live Operations panel under Workstation Diagnostic, with explicit recovery confirmations, saved phases and dependent operation IDs. Extend recovery to node removal and backup jobs; preserve restore checkpoints and adopt the exact backup after a lost result.
- Preserve human-readable audit titles and the errors filter in API responses; accept 3/5/10-minute alert intervals consistently in the API and workstation.
- Keep backup lists compact by omitting the version commit hash; show the absolute saved path after downloading a backup in the workstation.

- Observe an already dispatched update before rejecting a new target, close its local record when the backend confirms a terminal result, and omit misleading Resume instructions for completed failures. Include `--no-tui` in console resume hints and distinguish missing, terminal and non-stack tasks in the repair utility.

## 1.0.0-alpha.11 - 2026-10-14

- Reconcile collected systemd update units from exact durable terminal receipts, preserve unknown outcomes and persist confirmed completion so the next update can start. Add a guarded one-time repair for older controllers stuck after a confirmed rollback.
- Show maintenance explanations in Telegram and visible operation status during Workstation recovery, including controller update results.
- Require explicit installed shared paths in the repair utility, report specific evidence failures, and validate its real CLI path including parsed UUID arguments.
- Build new release artifacts from the current repository history; keep commit verification enabled for controller installation.

## 1.0.0-alpha.10 - 2026-10-14

- Replace repeated temporary-lease expiry service launches with a persistent worker, recover the old start-limit failure during runtime synchronization, and require an active expiry worker before retrieving credentials. Preserve expiry deadlines and read-only artifact retrieval.
- Fix multiline scrollbar proportions and bottom positions in notes, announcements and manual AWG parameters. Keep a full Back button in the AWG dialog without discarding the draft.
- Preserve temporary-configuration focus marker colors and Audit filter highlighting; keep the fixed controller channel grey on hover. Prevent late controller responses from replacing a different screen and use the full documentation list area.
- Simplify profile provisioning text, show creation failures in a separate result dialog, distinguish existing profiles, and allow the authenticated profile context endpoint used by editing.
- Restore Close and Dismiss controls on announcement notifications and show a known Telegram sender username. Format release notes into native headings, code and links with a full-release link, and omit the build hash from About.
- Offer alert intervals of 3, 5 and 10 minutes and preserve expanded policy details during Refresh. Mark journal archival as Skipped when no previous journals exist.
- Add regression coverage for intermittent artifact availability, expiry worker health, notification delivery, provisioning and terminal rendering. Validate with Python, PostgreSQL, Docker and Rust 1.90 suites and strict Workstation Clippy.

## 1.0.0-alpha.9 - 2026-10-14

- Pair accounts with one VPN profile, add a Telegram ID/known username provisioning wizard, and delete the account and profile together after confirmed node revocations. Freeze and Block preserve both entities. Preserve legacy runtime identities while normalizing profile ownership, retain inaccessible deletion receipts, and refresh the deleted user's bot screen with a new pending identity.
- Add optional manual backup names and manual/automatic labels. Automatic retention prunes only automatic backups; manual backups remain until explicitly deleted.
- Add cursor-aware multiline editing, scrolling and scrollbars to notes, announcements and manual AWG parameters; increase notes height and open manual AWG parameters from Advanced options.
- Keep the completed Telegram Quick Start available until Done or entry into the administrator menu, and correct the Workstation installation-defaults path.
- Display all administrator-rights outcomes in result dialogs, group profile actions in pairs, show permanent administrator access in a compact dialog, and protect administrator profile names from other administrators.
- Add human-readable Workstation audit actions and targets with All/Errors filters, persistent On highlighting, temporary-configuration focus markers, and Backspace navigation outside text inputs.
- Refine Telegram About and profile management text, and hide traffic statistics when collection is disabled.
- Name the regular Workstation Node Plane and the development version Node Plane (Dev), update launchers, documentation and API contracts, and add Python, PostgreSQL and Rust regression coverage.
- Account/profile migration 11 requires a reader supporting revision 11; application rollback to alpha.8 or older is unsupported after it is applied.

## 1.0.0-alpha.8 - 2026-10-13

- Fix a Clippy warning in the Workstation access screen that failed lint on all CI operating systems.
- Pin the repository Rust toolchain to 1.90.0, matching the version used by CI for local and CI lint consistency.

## 1.0.0-alpha.7 - 2026-10-13

- Complete Quick Start automatically and permanently, restore the empty Workstation panel after completion or dismissal, and preserve documentation list scroll positions.
- Organize Workstation operation journals and logs into private subdirectories, migrating existing files without losing resumable operations.
- Restore full Back and Cancel buttons inside wizards and dialogs, compact confirmations, support multiline notes and announcements, and refine templates, pagination and toggle colors.
- Fix profile viewing and creation, preserve wizard drafts, and retain permanent administrator access when editing profiles.
- Add privileged Workstation management of the Telegram token and administrator list with validation, confirmations and session checks.
- Deliver access decisions to administrator notices with the deciding administrator, Workstation attribution and Close buttons; refresh requester access messages and add Close/Dismiss notification actions.
- Refresh bot names in open menus, add administrator About links, show profile traffic counters, and correct protocol editing, repair navigation and extended node diagnostics.
- Update English and Russian documentation and add regression coverage for the coordinated backend, Telegram and Workstation changes.

## 1.0.0-alpha.6 - 2026-10-13

- Fix node overview during active bootstrap and settings application after protocol cleanup; recover old cleanup receipts without replaying operations and inspect the retained agent separately.
- Persist Workstation Quick Start completion across administrator selection, remove its access checkpoint and add confirmed permanent dismissal to both clients.
- Add named documentation links to Telegram settings and a Workstation Documentation list with descriptions and browser opening.
- Edit Telegram node settings through table row links, keep AWG manual parameters and entropy actions separate, and return live port operations to their Ports screen with verified runtime ownership results.
- Compact Workstation text dialogs, use a small Back arrow before Continue/Confirm, dim the fixed controller channel, group matching-region templates and protocols in wide layouts, and redraw flag pairs after template movement.
- Use `node-plane` for stable Workstation data and `node-plane-dev` for dev; resolve local-node `.` addresses from installation profiles and show update versions with their dev/stable channel.

## 1.0.0-alpha.5 - 2026-10-13

- Protect administrator profiles from deletion, freezing, blocking and access revocation; apply a permanent all-server rule after protocol installation and retain the previous member policy on demotion.
- Refresh node removal progress through read-only status requests without repeating removal commands.
- Move Telegram administrator Settings to the last System row.
- Refresh backup, announcement, profile synchronization and deletion progress automatically; keep Server setup observation alive across recoverable agent outages and show these as waiting instead of a failed installation.

- Persist Quick Start completion separately for Telegram and Workstation; hide completed guides, remove NPW Close and disable its F1 shortcut after Done.
- Return Telegram Quick Start to the home menu with matching breadcrumbs; require completed AWG or VLESS configuration issuance before Telegram Done.
- Stop highlighting administrator Updates when its cached release is already installed or older.

## 1.0.0-alpha.4 - 2026-10-13

- Run release-helper Python preflight through the CI runner, keeping infrastructure suites separate from unit database doubles.
- Separate stable/dev Workstation installations, launchers, local data and self-update receipts; restrict installation, updates and controller management to the matching channel.
- Copy connection profiles between NPW channels without sharing SSH keys or operation journals; allow an explicit controller-channel transition through Node Plane Updates.
- Fix Back from unconfirmed installation management, align installation buttons and reuse the selected profile in Quick Start.
- Color the fixed channel field consistently, clarify server defaults and their menu path, and end NPW Quick Start at profile access; user devices and ordinary configurations remain in Telegram.

## 1.0.0-alpha.3 - 2026-10-13

- Return revoked Telegram administrators from open admin panels and drafts to the current member menu; recheck stale admin navigation before handling it.
- Refresh the open Telegram home menu after administrator promotion or demotion without requiring /start, including menus without breadcrumbs.
- Route the owner-bound Quick Start button to the guide.
- Keep background presence requests from replacing the event receiver of an active Workstation update.
- Show Quick Start progress beside the Workstation sidebar when no section is selected; refresh once per second only while visible.

## 1.0.0-alpha.2 - 2026-10-13

- Restore the previous AWG container correctly after deployment failure on Bash 5.2 by returning explicit Docker command status from the EXIT trap.
- Make migration and rollback fixtures independent of host SQLite capabilities and installed units; simplify AWG port selection for the CI clippy version.

## 1.0.0-alpha.1 - 2026-10-13

- Fix member profile Details navigation and unknown traffic counters; keep Updates/Backups directly under the Telegram admin panel, including error returns and backup catalog breadcrumbs.

- Restore manual AWG 3.1 profiles with shared validation, ordinary revisioned settings application and preservation of server/peer identities.
- Add Workstation snapshot download/import and restoration across a fresh controller for the same Telegram administrator.
- Defer definite Telegram rate-limit rejections durably while preserving unknown delivery outcomes.
- Add resumable Quick Start guides based on controller progress, and select stable installations in stable Workstation builds.

- Fix inherited public addresses in node editors, preserve omitted settings in API patches, keep AWG port selection consistent, honor the configured VLESS fingerprint, reject invalid settings before saving, and verify node environment and AWG preset/interface during settings recovery.

- Require full Python, PostgreSQL, Docker and native Workstation checks before release publication; commit driver/agent dependency lockfiles and use locked Rust builds.
- Refresh AWG 3.1 profile generation with independent padding, randomized headers and timing ranges; validate padding limits and rekey deadlines while preserving existing profiles on update.
- Fix Workstation announcements and account selection when creating profiles.
- Serialize Telegram interactions, refresh member menus after account changes, and confirm bot-name and request-message edits.
- Reconnect nodes during backup restoration and report partial recovery per node.
- Move Telegram Updates and Backups into the administrator menu.
- Detect controllers removed by another Workstation, color diagnostic totals and the farewell, and keep installation-default counters in one group.

## 0.4.3-alpha.91 - 2026-10-12

- Fence controller removal verification before shutdown and stop polling the deleted backend after removal.
- Clear removed installation state and stale failed systemd unit state.
- Recognize managed VLESS listeners during diagnostics, scroll all server checks, and preserve Appearance scroll during theme changes and Settings hover.

## 0.4.3-alpha.90 - 2026-10-12

- Replace server-settings pagination with scrolling and an adaptive two-column parameter grid.
- Keep all parameters reachable with arrow keys and reserve only one row for Save and Reset draft.

## 0.4.3-alpha.89 - 2026-10-12

- Remove user-device management from Workstation administration and add custom access durations with H/D/M/Y units.
- Keep selected subsection labels visible and reserve footer space only for available actions.
- Group node diagnostics, reinstall and removal under Maintenance; include configured TCP/UDP port checks and colored diagnostic statuses.

## 0.4.3-alpha.88 - 2026-10-12

- Add a Windows Start menu shortcut for Workstation, launching in Windows Terminal when available and removing it on uninstall.

## 0.4.3-alpha.87 - 2026-10-12

- Preserve mouse hover after clicks and repaint it after control geometry changes.
- Reserve a disabled Traffic collection button while policy settings are loading.

## 0.4.3-alpha.86 - 2026-10-12

- Include the member username in profile navigation and simplify Workstation node-creation guidance and inline navigation.
- Scroll long wizard choices together with their footer and use available space in node settings and diagnostics.
- Refresh server cards after setup, follow temporary configuration progress, and restore the originating server card on Back.

## 0.4.3-alpha.85 - 2026-10-12

- Color diagnostic status markers and add labeled choices for automatic checks, backups and alerts.
- Hide disabled automation preferences, format backup sizes, and simplify Basic settings and draft dialogs.
- Keep draft actions together, preserve scrolling after reset, and use the full list height.
- Require a Workstation restart after updating the local application and simplify its header and exit prompt.
- Keep automatic update choices in the selected theme color during mouse navigation.

## 0.4.3-alpha.84 - 2026-10-12

- Simplify Updates to saved results and failed-node recovery, with one-hour, six-hour and daily automatic checks.
- Give Workstation workflows action-specific confirmations and persist diagnostic recovery preferences.
- Load controller statistics when Workstation returns automatically to Node Plane Settings after an operation.

## 0.4.3-alpha.83 - 2026-10-12

- Include known Telegram usernames in Workstation registration identities and simplify administrator Overview with conditional tasks, alerts/requests and an inline refresh timestamp.
- Offer available Workstation updates on the startup idle screen after connection checks finish, and use action-specific confirmation titles.

## 0.4.3-alpha.82 - 2026-10-12

- Fix Workstation controller removal confirmation requests and keep removal progress updating across preparation and shutdown.
- Show dedicated finish/cancel actions during controller removal and allow cancellation before final shutdown confirmation.
- Fix the Workstation formatting check for expanded installation diagnostics.

## 0.4.3-alpha.81 - 2026-10-12

- Extend installation diagnostics with bounded live server checks and explicit coverage in Telegram and Workstation.
- Check published database migration checksums and report agent/protocol/resource issues without replaying operations.

- Merge Telegram Status into an expanded administrator Overview with maintenance tasks, alerts, backup policy and known update state.
- Expand read-only server probes with selected protocol/container checks, configured ports, host resources, runtime drift and observation timing.

- Hide empty request and problem-node rows in Telegram administration summaries.
- Create the first AWG device only when a user requests its configuration, prompting for a device name.
- Generate AWG configurations asynchronously with automatically refreshed waiting screens and a Back action to the server list.

- Use Transport → TCP/XHTTP navigation on VLESS configuration screens and remove the redundant artifact-name heading.

- Show automatically refreshed node settings application progress and reopen active operations instead of offering duplicate apply commands.
- Publish settings application stages from the agent through the backend operation summary.
- Simplify the member profile and move account information, configuration statistics and server breakdown into Details.

- Cache successful version catalogs for 60 seconds when browsing update pages, while validating update targets against a fresh catalog.
- Overlap empty Telegram callback acknowledgements with screen loading and retain synchronous text, alert and URL responses.
- Record screen submission latency separately from total update-handler duration.

## 0.4.3-alpha.80 - 2026-10-12

- Include the Telegram credential validation library in controller installation archives.
- Exercise packaged installer startup and runtime library completeness in release CI.

## 0.4.3-alpha.79 - 2026-10-12

- Add opt-in action and stage timings for Telegram, backend API and Workstation.
- Correlate backend calls with request IDs and separate SSH forwarding, HTTP response, decoding and TUI drawing durations.
- Document measurement setup, timing boundaries and CPU sampling with pidstat.

## 0.4.3-alpha.78 - 2026-10-12

- Verify the persistent worker service after installation and report Telegram polling conflicts explicitly.
- Stop Telegram polling on token conflicts to prevent competing installations from alternating user state.
- Validate Telegram credentials before installation and skip already installed host prerequisites.
- Fix PostgreSQL server settings saves and avoid applying runtime settings for metadata-only changes.
- Keep node connection transport fixed in Telegram settings.
- Fix Rust dependency cache archives containing hardlinked Cargo outputs.

## 0.4.3-alpha.77 - 2026-10-11

- Return to Node Plane Updates with refreshed status after closing a successful update result.
- Remove the Back arrow from the Updates root while retaining navigation in nested screens.

## 0.4.3-alpha.76 - 2026-10-11

- Reuse PostgreSQL connections in the backend API.
- Wake Telegram notification delivery after committed queue changes, with periodic fallback and automatic listener reconnection.
- Preserve delivery authorization, claim/ack idempotency and interrupted-send handling.
- Add a To latest action to Workstation when a controller update is available.
- Fix the Workstation Clippy check for operation labels.

## 0.4.3-alpha.75 - 2026-10-11

- Run the backend worker continuously, reuse PostgreSQL connections, and wake queued jobs after commit.
- Preserve recovery locks and restore the appropriate worker service during updates and rollback.
- Expand Workstation administration, notifications, server settings, and update management.
- Improve Workstation navigation, scrolling, focus restoration, and file-based themes.
- Rewrite installation and user documentation in English and Russian.

## 0.4.3-alpha.74 - 2026-10-10

- Preserve server artifact inputs while cargo-dist packages the release.
- Add server settings editing in Workstation, including protocol options and region access confirmation.
- Warn before discarding unsaved profile and server changes in Telegram and Workstation.

## 0.4.3-alpha.73 - 2026-10-10

- Add Workstation Access: accounts, requests, profile settings, current/future access rules, devices and expiry presets.
- Add controller reset/removal and live installation checks in Workstation, with independent removal verification and a final goodbye screen.
- Refresh profile deletion progress automatically in Telegram and Workstation.
- Improve Workstation focus, field labels and button styling; mark unavailable actions with a cross.
- Preserve administrator profiles during controller reset and temporary configuration access after server metadata edits.
- Highlight available updates and active alerts; allow dismissing alert attention.
- Simplify controller diagnostics and cleanup screens, and restore update progress panels after bot restarts.
- Build server artifacts alongside Workstation and reuse Rust dependencies across release tags.

## 0.4.3-alpha.68 - 2026-10-10

- Add operation-specific diagnostics and recovery for profile access, server settings, agent installation and complete server setup, with administrator attribution and recovery history.
- Add controller diagnostics to Telegram and explain blocked operation causes and applicable next steps in both interfaces.
- Unify Server setup: prepare SSH in Workstation, install the agent, Docker and selected protocols; allow agent-only servers and install protocols later.
- Publish detailed installation progress and refresh active server cards and operations automatically.
- Fix Telegram panel recovery after Clear history, server metadata drafts, request notifications, SNI validation and navigation from Updates to server cards.
- Unify Workstation button layouts, spatial navigation, scrolling and appearance; keep Temporary configs centered and disable unreliable terminal QR rendering.
- Add QR delivery for temporary configurations in Telegram.
- Fix nullable SQL parameter typing in the PostgreSQL setup guard.

## 0.4.3-alpha.64 - 2026-10-09

- Run Workstation tests serially on Linux, macOS and Windows to avoid concurrent temporary-state locks.


## 0.4.3-alpha.63 - 2026-10-09

- Fix Workstation builds with Rust 1.90 and make expiry and SSH test fixtures reliable across operating systems.


## 0.4.3-alpha.62 - 2026-10-09

- Fix Workstation release builds on all platforms by using Rust 1.90, required by the QR image dependency.
- Align the declared minimum Rust version, release workflow and native Workstation checks.


## 0.4.3-alpha.61 - 2026-10-09

- Add temporary AWG/VLESS configurations with 12h, 1d and 3d durations, early revocation and management in Telegram and Workstation.
- Enforce temporary access independently of the controller and agent, with persistent expiry scheduling and guarded protocol startup.
- Verify agent failure recovery and two real Debian 12 systemd boots without resurrecting expired credentials.
- Fix AWG image builds on Debian 12 Docker 20.10.
- Preserve permanent and other active temporary configurations during expiry and restart.


## 0.4.3-alpha.60 - 2026-10-09

- Build Workstation releases with cargo-dist for Linux, Windows and macOS, including shell and PowerShell installers.
- Add the shared node creation wizard to Workstation, with clean template codes and the single-local-node limit.
- Simplify RU/EN bot text and show concrete cleanup consequences with affected profile counts.
- Keep profile protocols on one compact line with or without traffic statistics.
- Reorganize Installation defaults with a collapsed summary, editable parameter links and complete Xray defaults.


## 0.4.3-alpha.39 - 2026-10-05

- Remove the retired PTB monolith, dependency and obsolete controller Docker entrypoint.
- Consolidate development work into one plan and refresh architecture/API references.
- Make traffic accounting an administrator-controlled installation policy for all profiles.
- Add monthly admin profile totals and a separate regional, paginated server traffic view.
- Show protocol usage in compact bullet-separated rows.
- Put config help/QR above Show/Hide URI controls, with copyable text and downloadable
  files outside collapsible blocks for the reported Telegram iOS interaction issue.
- Compact AWG URI encoding without changing its compressed wire format.
- Parse local-agent discovery config as TOML and retain explicit target precedence.
- Display small backup sizes accurately and keep installation/update paths on the new stack.


## 0.4.3-alpha.19

- Complete native backend/aiogram administration: node maintenance and removal,
  announcements, alerts, opt-in traffic statistics, updates and backups.
- Add confirmed controller reset and full systemd uninstall with ownership checks.
- Remove the backend SQLite implementation; retain agent command journals.
- Limit Telegram commands to start, help, id, version and administrator status.
- Keep RU/EN localization and backend authorization across the migrated screens.

## Unreleased

- Separate stable/dev Workstation installations, launchers, local data and self-update receipts; restrict installation, updates and controller management to the matching channel.
- Copy connection profiles between NPW channels without sharing SSH keys or operation journals; allow an explicit controller-channel transition through Node Plane Updates.
- Fix Back from unconfirmed installation management, align installation buttons and reuse the selected profile in Quick Start.
- Color the fixed channel field consistently, clarify server defaults and their menu path, and end NPW Quick Start at profile access; user devices and ordinary configurations remain in Telegram.

## 0.4.1-alpha.22 - 2026-09-26

- Restore missing AWG peers and Xray users before issuing configs after a clean reinstall.
- Offer native AmneziaVPN `.vpn` files alongside `.conf` exports for AWG 3.1 clients.
- Return to pending requests or the admin menu after approving/rejecting access; ignore repeated decisions.
- Keep detailed AWG issuance errors in logs rather than user messages.

## 0.3.9 - 2026-04-04

### Added
- database backups with restore support, retention settings, and backup cleanup
- Telegram alerts for node and service health
- branch-aware updates with version selection and release cleanup
- runtime drift detection and runtime sync for managed nodes

### Changed
- installer now supports branch-based installs and reuses existing releases when possible
- admin settings now group operational tools more logically, including updates, backups, SSH key, cleanup, and alerts
- server maintenance and advanced runtime screens are more structured and easier to navigate
- profile, server, updates, and backup screens were refined for clearer mobile-friendly UX

### Fixed
- update version detection now reads the installed release correctly instead of the source checkout
- cleanup and uninstall flows now remove managed runtime state more reliably on both the bot host and remote nodes
- bootstrap now waits for apt locks instead of failing immediately on unattended upgrades
- SSH host key onboarding now works for first-time SSH connections to managed nodes
- alerts now trigger on the first failed check and use current metrics in recovery notifications

### Security
- destructive cleanup and uninstall actions now require typed confirmation
- secrets and sensitive runtime output are handled more carefully
- local and remote cleanup flows remove managed containers, images, runtime files, and related state more consistently

## 0.1.0 - 2026-04-03

### Added
- initial stable Node Plane release
- Telegram bot for VPN access management
- profile and server management flows
- Xray and AWG support
- traffic collection and diagnostics
- admin tools for requests, announcements, SSH key setup, and updates
