# Planned Vaultwarden support

Vaultwarden is the first planned additional service. This document describes design work; Node Plane does not currently provide a Vaultwarden lifecycle adapter.

## Scope

Manage a curated Vaultwarden instance on a selected node: creation, configuration, health checks, updates, backups, restore and removal. Reuse Node Plane's account identity, authorization, durable operations and audit.

Vaultwarden is a service with its own enrollment and data model. Do not represent its access as a VPN protocol grant or store its user credentials in VPN profiles.

## Design stages

1. Define instance configuration: hostname, TLS/reverse-proxy boundary, persistent data paths and secret handling.
2. Define invitation/enrollment, administrator actions, access revocation and the relationship to a Node Plane account.
3. Implement an agent adapter with validated inputs, resource ownership, durable command identity and health observations.
4. Add backend endpoints and worker operations for deployment and enrollment.
5. Add equivalent administrator flows in Workstation and Telegram.
6. Implement backup/restore and version compatibility before enabling updates/removal.

## Acceptance

A disposable instance must pass deployment, invitation, interrupted-operation recovery, restart, update, backup restore and complete owned-resource removal. Document which actions revoke login/session access and which preserve user data. Keep Vaultwarden secrets out of logs and general audit records.

Select concrete container versions and proxy/TLS integration during implementation. A working deployment alone is not sufficient to declare support.
