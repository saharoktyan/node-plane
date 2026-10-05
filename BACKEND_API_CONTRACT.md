# Backend API and authorization reference

Updated: 2026-10-05. This reference retains the identity, trust and command
contracts from the original API design; migration logs and obsolete next-step
lists have been removed. The implemented routes and payloads are authoritative
in `app/backend/http_api.py` and [backend reference](app/backend/README.md).
Remaining work is tracked only in [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md).

## Identity and authorization

Accounts have immutable UUIDs and independent external identities. Telegram
uses a numeric user ID for identity binding, never username or chat title.
Accounts and VPN profiles are separate backend entities even though Telegram
presents them together. Profiles have independent runtime identity and optional
owners; renaming a display label does not rotate runtime credentials.
VPN grants associate profile, node and protocol; Xray transport selection is
not a separate person or service account.

Authorization combines authenticated principal, permitted delegation, actor,
permission, resource ownership and resource state. Hiding UI controls does not
authorize an action. Initial administrators are explicitly bootstrapped locally;
the first public visitor never becomes an administrator. Administrative role
changes require authorization and preserve last-admin protection. Administrators
have permanent account access; ordinary VPN issuance still checks profile and
grant validity. Sensitive artifact reads require their own permission.

## Credentials and trust

The backend normally listens on localhost, with authenticated requests even
there. Opaque bearer credentials are distinct from the Telegram Bot API token;
only secret hashes are stored server-side. Credentials have principal type,
scopes and expiry/revocation state. Full secrets are returned on creation only
and must not appear in URLs, logs or ordinary operation responses.

A trusted Telegram adapter resolves identities and delegates using its permitted
scopes and `X-Node-Plane-Telegram-User-ID`. The numeric header alone proves
nothing without an authorized adapter credential. Unknown identity registration
is explicit. Account credentials identify their own actor and cannot use adapter
headers to impersonate others. Service actors have narrowly defined permissions.

Compromise of the Telegram adapter credential compromises its delegation trust;
UI restrictions cannot fix that. Future CLI/browser clients require their own
credentials, not reuse of the adapter token. Revocation and changed access are
checked on subsequent requests and again before new remote dispatch. An already
sent mutation is not automatically cancelled by credential revocation.

## API behavior and durable commands

Use versioned `/api/v1` resources, validated request/response models, UTC times,
stable identity and cursor pagination. List/status reads do not include private
keys. Error responses contain machine-readable codes and request IDs rather
than raw SQL, shell output or gRPC tracebacks; clients localize codes.

Mutations use expected revisions and idempotency keys as required by each
endpoint. A repeated logical command retains its identity; conflicting payloads
under the same key are rejected. Repeated requests still check current access,
and stale artifacts cannot bypass revoked access. Desired changes and durable
tasks are recorded before remote dispatch. Worker leases do not authorize blind
replay of unknown remote outcomes. Parent business operations preserve per-node
results and child execution identity.

Authentication, ownership, last-admin protection, revision conflicts, duplicate
commands and ambiguous recovery must remain testable independently of Telegram.
Actual PostgreSQL concurrency evidence is separate from policy tests using SQL
fakes. Independent component compatibility, retention and recovery limits are
open closure tasks, not implied by the API version number.

## Traffic accounting

An admin-only global switch controls collection and every traffic read. Members
cannot independently enable/disable collection. `/me` reports availability;
obsolete `traffic_consent` input is rejected. Initial schema setup removes old
consent preferences. Global generation fences discard in-flight responses when
the policy changes; paused intervals are excluded after re-enabling.

The owned profile summary requires ownership; the administrative profile summary
requires `profiles.manage`. Both return no traffic while globally disabled and
otherwise report waiting/current/unknown, UTC monthly totals and per-node/protocol
counts. Ownerless profiles can also be collected. Ownership changes cannot
transfer historical usage to another account. Native counter reads do not reset
counters or collect browsing history. Accounting is approximate, not a completed
quota/billing enforcement contract.

## Controller cleanup (2026-10-01)

`maintenance.manage` controls `/api/v1/system/cleanup`. GET reports supported
installation ownership, account/profile/node counts and the latest job. POST
`/plans` accepts only `{action: "reset"|"remove", cleanup_nodes: boolean}` and
returns an expiring operator/principal-bound plan plus the exact confirmation
phrase. POST `/jobs` accepts `{plan_id, confirmation_phrase}` and requires a UUID
`Idempotency-Key`. Reusing a consumed plan returns its original job; changed or
expired plans are rejected before effects. Paths cannot be supplied by API clients.

GET `/jobs/{id}` reports sanitized status, phase, snapshot ID and individual node
results. POST `/retry` retries a blocked safe step; POST `/abort` stops before the
irreversible controller phase. Both suffixes are under `/jobs/{id}`. POST
`/jobs/{id}/shutdown-ack` is reserved for the confirming actor/principal after the
client delivers the final screen. Full removal waits in `awaiting_shutdown` until
this acknowledgment. Systemd logs own the final removal result; a queued shutdown
is not proof of successful removal. Uncertain launch is blocked without replay.

Cleanup fences other mutations and transport claims. Reset keeps the current
administrator and active credential; one pre-reset snapshot remains on disk.
Full removal clears controller data, including an external PostgreSQL deployment,
and deletes only installer-owned resources. See [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md) for remaining retention and
independent node verification work.
