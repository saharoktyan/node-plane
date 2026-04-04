# Changelog

## Unreleased

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
