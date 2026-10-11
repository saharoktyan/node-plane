# Roadmap

This file lists planned work and acceptance gates. User guides describe implemented workflows; release history belongs in [CHANGELOG](../CHANGELOG.md).

## Interactive Quick Start Guides

Create separate guided flows for Workstation and an administrator's first `/start` in Telegram. Each uses the actual interface to configure installation defaults, create the first local node, run setup and grant profile access.

The guide must support existing installations, skipping completed actions, resuming after interruption and leaving without losing ordinary navigation. Telegram should demonstrate the clickable location path. Tutorial completion must reflect successful operations, not merely visited screens.

## Release acceptance

Verify clean installation and update on disposable hosts, complete local/SSH node setup and removal, backup restore, profile revocation and temporary-access expiry. Test notifications/alerts and automatic update checks on real installations.

Run Workstation acceptance on Linux, macOS and Windows: non-admin local use, PATH, SSH/session reuse, clipboard, focus, scroll and resize. Check Telegram on desktop, Android and iOS. Record untested cases explicitly before a stable release.

## Future work

- [Vaultwarden lifecycle support](design/service-expansion.md).
- Evaluate established-session revocation in the Xray runtime. Current VLESS revocation blocks new connections; existing connections may remain until disconnect. Any implementation must preserve other users' sessions and shared listeners.
- Define safe retention for long-lived command journals without losing recovery or duplicate protection.
