# Changelog

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
