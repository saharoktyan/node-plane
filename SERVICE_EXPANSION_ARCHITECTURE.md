# Service expansion without a general-purpose orchestrator

Status: direction for the backend migration, 2026-09-28. This document
supersedes the earlier assumption that every non-VPN feature belongs to a
licensed Pro edition. It does not claim Matrix or Bitwarden support exists.

## Product boundary

Node Plane is a self-hosted control plane for a small, curated set of privacy
services. VPN access is the first implemented service. Matrix and a self-hosted
password manager are candidates, to be evaluated one at a time after the VPN
and backend migration are stable. New VPN protocols should fit the same model.

Arbitrary user-defined workloads, a general YAML editor, scheduling, and a
Kubernetes-like Telegram interface are outside this product boundary. Saharo
may eventually provide deployment primitives behind a service adapter, but
Node Plane must not depend on Saharo or merge its databases to complete the
current migration.

## Ownership

The backend owns accounts, external identities, authorization, node and
service-instance records, desired access, service catalog capabilities, and
business operations. Telegram is one client of the backend API. The driver and
node agent execute typed, idempotent commands and report observations; they do
not decide who may use a service. A service adapter owns its service-specific
configuration, reconciliation, onboarding and revocation rules.

The existing `backend_profiles`, `backend_grants` and VPN config endpoints are
the first VPN-specific slice. Their `awg|xray` protocol check is not the future
catalog of every supported service. Do not add `matrix` or `bitwarden` as a
value in that column, or use a VPN profile as a universal service account.

The reusable resource concepts are:

| Concept | Meaning | VPN example | Other-service example |
| --- | --- | --- | --- |
| Account | A person known to the backend | Telegram-linked user | Same person requesting Matrix access |
| Node | A managed machine | VPS running Xray/AWG | Host running a homeserver |
| Service instance | A deployed, addressable service | Xray on a node | One Matrix homeserver |
| Access enrollment | Account's desired relationship to an instance | VPN profile grant | Matrix registration or vault invitation |
| Operation | Durable requested change and observed result | Add/revoke VPN peer | Issue/revoke invitation |

These are conceptual boundaries, not a mandate to create five generic tables
now. The first non-VPN vertical slice should prove which fields and behaviors
are truly shared. Keep service-specific data in typed records owned by the
adapter rather than a universal JSON blob that bypasses validation.

## Adapter contract

Adapters expose capabilities, not a promise that every service supports every
action. Useful capabilities include deployment, health observation, update,
backup/restore, account onboarding, access suspension and revocation. The
backend authorizes each action and records an idempotency key and desired
revision before dispatch. An adapter returns structured outcomes and an
explicit `unsupported` result for absent capabilities.

Installation and access are separate workflows. A healthy service instance
does not imply that a person has an account there. VPN credential issuance,
Matrix registration and password-manager invitations have different lifecycle
and security rules. The common API may expose the request, decision and status;
the service adapter defines the actual enrollment steps and artifacts.

The bot may deliver an invitation or a short-lived enrollment link. It must
not collect a password-manager master password, store vault contents, or turn a
Telegram identity into an application login without the service's own consent
and authentication flow. Secrets are kept in a dedicated credential boundary,
never in operation list responses or Telegram logs.

## Deployment boundary

For a curated service, a versioned manifest can describe image, ports,
persistent volumes, health checks, backup requirements and upgrade policy.
This manifest is maintained with the adapter, not edited as arbitrary YAML in
Telegram. A future Saharo integration would translate such a typed deployment
request into Saharo's API; it would not make Saharo's scheduler or resource
schema part of Node Plane's public access API.

An adapter is not ready to ship merely because its container starts. It needs
an update and rollback path, persistent-data backup and restore, service-specific
health checks, credential rotation and an honest uninstall contract. This is
especially important for chat and password storage.

## Near-term implementation order

1. Finish the VPN backend/API migration and preserve its existing behavior.
2. Keep shared account, authorization, node and operation APIs independent of
   `awg|xray`; leave VPN profiles and config issuance explicitly VPN-specific.
3. Before implementing a second service, write its lifecycle and access
   contract with real user journeys and failure cases. Add only the shared
   service-instance/enrollment primitives that contract needs.
4. Implement and validate one complete non-VPN vertical slice, including
   deployment, onboarding, revocation, update and recovery, before adding more.

## Commercial scope

There is no Pro edition, license server or paid-feature gate in the near-term
delivery plan. Existing Core/Pro sketches remain historical options and must
not constrain the shared API or the usefulness of the free installation.
Possible funding approaches can be evaluated later against actual users and
maintenance costs; they are not a reason to add licensing code now.
