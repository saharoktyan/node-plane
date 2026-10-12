# Changelog

## Unreleased

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
