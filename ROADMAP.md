# Roadmap

This document outlines the planned direction for upcoming Node Plane releases.

It is intentionally lightweight:
- version-oriented
- focused on major themes
- flexible enough to change as implementation details become clearer

## 0.4.x — Storage and Control Plane

Primary goal:
- improve reliability, data model flexibility, and execution architecture

Planned work:
- migrate storage from SQLite to PostgreSQL
- prepare and validate a migration path from the current SQLite schema
- introduce a Rust control service alongside the bot
- introduce a node agent that runs on managed servers
- establish a gRPC channel between the control service and each node
- reduce dependence on shell-script-based orchestration for runtime actions
- adopt Xray API for dynamic user and traffic operations where it is a better fit than config rewrites
- improve latency and consistency of node operations

Why this comes first:
- PostgreSQL is a better foundation for more complex account and device models
- the current shell-driven runtime flow works, but is not the long-term architecture
- moving to a dedicated control layer will make future features easier to build and maintain

Expected direction:
- keep config files as the source of truth for static Xray runtime setup
- use Xray API for dynamic operations such as user changes and live traffic/stat access

## 0.5.x — Devices and Access Model

Primary goal:
- move from user-level configs to device-level access management

Planned work:
- support multiple devices per account
- issue configs per device instead of per user
- revoke and regenerate access at device level
- track usage per device in overview screens
- add per-user device limits

Notes:
- this is especially important for AWG, where per-device configs are functionally important
- for Xray, per-device separation is also useful for better traffic visibility and account management

## 0.6.x — Access UX

Primary goal:
- make access management and config retrieval significantly easier for both admins and users

Planned work:
- one-click access grants by region
- one-click access grants across all matching servers and protocols
- simpler bulk access workflows for admins
- more convenient config retrieval for users
- clearer protocol and server selection flows
- less friction in request approval and access issuance

## Later Ideas

These are likely directions, but not yet assigned to a specific release:
- deeper replacement of shell-script workflows with structured control-plane operations
- richer observability and health reporting
- more granular admin tooling for bulk operations
- broader traffic and usage reporting
- further UX cleanup in both admin and user flows

## Guiding Principles

The roadmap follows three broader priorities:

1. Architecture
- PostgreSQL
- Rust control service
- node agent
- gRPC-based coordination

2. Access model
- devices instead of only users
- per-device limits
- clearer access ownership and traffic attribution

3. UX
- simpler operator workflows
- simpler config delivery
- less manual repetition in day-to-day administration
