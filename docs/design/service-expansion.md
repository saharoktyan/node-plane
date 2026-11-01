# Non-VPN service evaluation example

This document is an exploratory design example for evaluating a possible managed service. Node Plane does not currently provide a Vaultwarden lifecycle adapter, and this document does not commit the project to implementing Vaultwarden. The public [roadmap](../roadmap.md) treats Vaultwarden and Grafana/Loki as possible examples while selecting one non-VPN service for an architecture exercise.

## Scope to evaluate

A managed service candidate would need deployment, configuration, health checks, updates, backups, restore and owned-resource removal on a selected node. It should reuse Node Plane's authorization, durable operations and audit without turning service access into a VPN protocol grant.

A candidate service has its own enrollment and data model. Its user credentials, data and service secrets must remain separate from VPN profiles and controller infrastructure credentials.

## Evaluation stages

1. Define instance configuration, persistent data paths and secret handling.
2. Define invitation, enrollment, administrator actions, revocation and the relationship to a Node Plane account.
3. Implement a validated Agent adapter with resource ownership, durable command identity and health observations.
4. Add backend and Worker operations for deployment, enrollment, update and removal.
5. Add administrator flows in Workstation and Telegram only if the service model warrants them.
6. Implement backup/restore and version compatibility before enabling updates or removal.

## Acceptance

A disposable candidate instance must pass deployment, invitation, interrupted-operation recovery, restart, update, backup restore and complete owned-resource removal. Document which actions revoke login/session access and which preserve user data. Keep service secrets out of logs and general audit records.
