# Roadmap

This file lists planned work and acceptance gates. User guides describe implemented workflows; release history belongs in [CHANGELOG](../CHANGELOG.md).

## Interactive Quick Start Guides

Workstation F1 and the administrator's Telegram home menu open guides derived from controller state. They navigate to existing screens for defaults, node creation, setup and grants; configuration delivery uses Telegram. Completed operations are skipped on reopening. Remaining guide work: visual acceptance across supported clients and improving the first-installation connection prompts after tester feedback.

## Release acceptance

Verify clean installation and update on disposable hosts, complete local/SSH node setup and removal, backup restore, profile revocation and temporary-access expiry. Test notifications/alerts and automatic update checks on real installations.

Run Workstation acceptance on Linux, macOS and Windows: non-admin local use, PATH, SSH/session reuse, clipboard, focus, scroll and resize. Check Telegram on desktop, Android and iOS. Record untested cases explicitly before a stable release.

## Future work

- [Vaultwarden lifecycle support](design/service-expansion.md).
- Evaluate established-session revocation in the Xray runtime. Current VLESS revocation blocks new connections; existing connections may remain until disconnect. Any implementation must preserve other users' sessions and shared listeners.
- Define safe retention for long-lived command journals without losing recovery or duplicate protection.
