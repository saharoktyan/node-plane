# 1.0.0 acceptance

This checklist accompanies preparation of the first stable release. The current manual acceptance build is `1.0.0-alpha.3` in the dev channel; the target stable version remains `1.0.0`. This document does not record publication or approval of the manual checks below.

## Automated evidence

Run `scripts/run_ci_tests.py` separately for unit, PostgreSQL and Docker suites. Each must finish without unexpected skips. Run the three Rust suites with committed lockfiles, Workstation strict clippy, shell syntax checks and workflow validation. CI additionally checks Python 3.11/3.12 and native Workstation builds on Linux, macOS and Windows.

Regression coverage includes inherited public addresses after node edits, manual AWG profile application and reset without identity changes, snapshot export/import and restore with a replacement administrator UUID, Telegram notification flood retry, Quick Start progress, Updates/Backups navigation and profile Details with unavailable traffic counters. Docker acceptance checks actual AWG traffic, Xray runtime persistence and agent process interruption.

Build the server stack in `scripts/Dockerfile.release` (Debian 12). Verify the controller manifest, checksums and glibc floor. Rebuild final artifacts from the clean candidate commit; archives built from a working tree are development verification artifacts. Keep the commit, test output and artifact checksums with release evidence.

### Local verification on 2026-10-13

- Python 3.12.13: 1290 unit/contract tests, 120 real PostgreSQL tests and 17 Docker runtime/fault tests passed with no skips.
- Rust: 265 Workstation, 34 driver and 15 agent tests passed. Workstation strict clippy and formatting passed.
- All shell scripts passed `bash -n`. Linux Workstation release binary reports `node-plane 1.0.0`.
- Previously built Debian 12 server artifacts passed checksum and controller-manifest verification; the new runtime modules are present. These working-tree archives must be rebuilt from the final candidate commit before publication.
- GitHub CI and manual checks below remain pending. This local run covers Python 3.12 and Linux; it does not certify Python 3.11 or actual Windows/macOS behavior.

## Manual checks still required

- [ ] Install a fresh controller from the stable channel on the supported clean host; complete Quick Start through both Telegram and NPW, close/reopen it midway and verify progress resumes.
- [ ] Create local and SSH nodes; issue VLESS TCP, VLESS XHTTP and AWG 3.1 configurations, import them into real clients and confirm traffic.
- [ ] Change the public address and protocol overrides; verify newly issued configurations use the intended endpoints.
- [ ] Paste a complete manual AWG profile in both interfaces, apply it, issue a configuration and confirm traffic; return to automatic generation.
- [ ] Check Telegram profile Details, breadcrumbs, every Back button in Updates/Backups and highlight only Updates when a release is available.
- [ ] Export a snapshot to another machine, install a replacement controller with the same Telegram administrator, restore SSH access, import and restore; inspect every node result and issue a fresh configuration.
- [ ] Verify notification handling of a real Telegram flood response when practical. Do not induce floods in a production chat; automated retry coverage alone does not certify every interactive bot action.
- [ ] Exercise NPW on actual Linux, Windows and macOS terminals: startup, SSH, paths, clipboard/paste, scrolling and update behavior.
- [ ] Verify controller restart/reboot and rollback from the supported previous development installation using a disposable host. Keep the previous runtime and a backup.
- [ ] Run GitHub CI for the candidate commit; confirm all required jobs pass and release assets match that commit.

## Release boundaries

The initial release does not certify large deployments. Limited concurrency, automatic provisioning from an exported snapshot and VPN client subscriptions remain deferred. Current controller recovery requires a new installation followed by explicit import and restore. Downloaded snapshots must be stored privately because they contain configuration data.

After manual acceptance, transfer the tested candidate to `main`, rebuild from the final clean commit and create/push the release tag deliberately. Creating or pushing a tag starts publication; preparing this checklist does not do so.
