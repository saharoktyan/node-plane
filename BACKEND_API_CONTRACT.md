# Backend API and authorization reference

Updated: 2026-10-07. This reference retains the identity, trust and command
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

## Node creation and installation defaults

`GET /api/v1/nodes/{node_key}/maintenance` includes `affected_profiles`, the
number of distinct profiles currently granted access to the server. Full cleanup
uses this count in its confirmation, counting each profile once across protocols.

`GET /api/v1/nodes/creation-options` requires `nodes.manage` and returns
`local_available` and the portable installation defaults. Creating a node or
changing its connection to local enforces the single-local-node rule inside
the serialized maintenance transaction. Disabled and deleting nodes retain
the slot until their registry record is removed. An omitted connection type
counts as local; SSH nodes do not occupy the local slot. Conflicts return
`409 local_node_exists`, including concurrent requests.

`GET /api/v1/system/installation-defaults` and `PUT` on the same resource
require `settings.manage`. Responses include a revision and its quoted ETag;
PUT requires `If-Match`. The payload contains `protocols`, `xray_transports`
and `settings`. At least one protocol must remain enabled, and enabled Xray
requires at least one transport. Settings accept the AWG preset, automatic or
manual port policy, manual port and interface, plus VLESS SNI, fingerprint,
TCP/XHTTP ports and XHTTP path. Host addresses, generated keys and other
node-specific values are not portable defaults. Manual AWG mode requires a
port; automatic mode must omit a fixed port.

PUT replaces the policy without modifying existing nodes. An identical retry
against the immediately previous revision returns the stored result; a
conflicting stale edit returns `412 revision_conflict`. Defaults are included
in configuration backups. Node creation inherits portable settings, with
explicit node values taking precedence. API clients still supply their chosen
protocols. The Telegram wizard reads defaults at entry, presents the effective
choices in its review and submits those choices as explicit overrides.

## Persistent access to future servers

Regions are catalog records with stable UUIDs, separate from node display labels.
`GET /api/v1/regions` requires `grants.manage` and supports cursor pagination.
Admin node representations include `region_id` and `policy_eligible`. The latter
is the authoritative eligibility flag for client-side access previews, including
maintenance and removal state. Removing the last node in a
region preserves the region and its policies for future installations. Creation
and region edits resolve the supplied label using Unicode normalization, case
folding and whitespace normalization.

`GET /api/v1/profiles/{id}/access-policy` requires `grants.manage`. It returns
the profile revision/ETag, `explicit_grants`, `rules`, `exclusions` and computed
`inherited_grants`. Existing grants remain explicit until a policy is configured.
`PUT` on the same path replaces explicit sources and policy together, requiring
`If-Match` and a UUID `Idempotency-Key`. It returns the normal profile command
result with the effective grants and durable operation ID.

Profile creation and profile `PATCH` also accept an `access_policy` object with
the same explicit sources, rules and exclusions. This allows creation/approval
wizards to save access and `expires_at` in one transaction and one profile
revision. Combining `grants` with `access_policy` is rejected (creation permits
an empty default grants list). Invalid profile fields roll back policy changes.
These commands additionally require `grants.manage` and an approved administrator.

A rule contains `scope` (`all` or `region`), `region_id` (null for `all`, a catalog
UUID for `region`) and a nonempty selection of `awg`/`xray` protocols. Only one
rule per scope/region is allowed. Explicit grants and exclusions contain
`node_key` and `protocol`. Exclusions remove inherited access only; independent
explicit access still wins. Effective access is the union of manual grants and
matching rules, so removing one overlapping source does not revoke another.
The existing snapshot grants endpoint replaces manual sources while retaining
the policy; clients editing policy-enabled profiles must read the sources rather
than copying the effective list back as manual grants.

New nodes gain derived access after confirmed bootstrap, for the protocols they
actually provide. Previously installed nodes retain desired access while runtime
maintenance temporarily disables issuance. Freeze and expiry gate execution and
issuance; deletion removes the policy. Changes use the existing profile revision
fences, device-aware outbox and uncertain-outcome recovery. Node drain removes
its concrete manual sources and prevents policy re-enrollment while retaining
all-server and regional rules.

`GET /api/v1/nodes/{key}/region-access-preview?region=...` requires `nodes.manage`
and `grants.manage`. It reports profiles whose policy access would change,
including future activation of an unbootstrapped node. A region edit affecting
access requires `grants.manage` and `confirm_access_change: true`; otherwise it
returns `409 region_policy_review_required`. The command recomputes the effect
inside the serialized transaction rather than trusting a prior preview.

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

The administrator node overview (`GET /api/v1/nodes/{key}/overview`,
`nodes.manage`) includes `traffic` for the current UTC month. It is null when
accounting is disabled; otherwise it contains `month`, `status`, nullable
`total_bytes` and per-protocol nullable upload/download totals. Missing samples
are not zeroes. Anonymous node totals survive profile/device retirement and are
reset with traffic baselines during backup restore. Node removal clears them.

## Update auto-check

Update preferences accept `auto_check_enabled` and
`auto_check_interval_minutes` (15, 60, 360 or 1440; default 60), alongside branch
and development-track selection. The overview returns the saved interval.
Automatic checks enqueue one `update_available` notification per version and
channel for approved Telegram administrators. The existing alert claim/ack
transport delivers these independently of the server-monitoring switch, and
rechecks recipient authorization and the selected update channel. Payloads
contain `version`, `branch`, `dev_track` and optional bounded `changelog`.
Closing the separate notification leaves the active control panel untouched.

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

Release history retention is automatic after verified controller activation.
The current and actual previous working release are retained; manual release
cleanup routes and Telegram actions have been removed. Full installation
cleanup remains under `/api/v1/system/cleanup` with `maintenance.manage`.

## Device identity foundation (2026-10-07)

GET `/api/v1/profiles/{profile_id}/devices` uses the same parent-profile read
authorization as the profile endpoint. It returns `items` with device UUID,
profile UUID, display name, lifecycle status, revision and creation timestamp;
internal runtime identities and peer credentials are excluded.

POST the same path to create a named device; PATCH or DELETE
`/api/v1/profiles/{profile_id}/devices/{device_id}` to rename or revoke it.
Creation and rename accept `display_name`. All mutations require a UUID
`Idempotency-Key` and a quoted integer `If-Match`: the profile desired revision
for creation, the device revision for rename/delete. Creation returns 201,
rename 200 and deletion 202. Results contain the device, `profile_revision`,
nullable `operation_id` and `runtime_status`; ETag is the device revision.

Names contain 1–64 normalized characters and are unique within the profile,
ignoring case and repeated whitespace. Duplicate names return
`device_name_conflict` (409); invalid names return `invalid_device_name` (422).
Deleting devices reserve their names until confirmed retirement. Rename is a
metadata-only change. Create/delete increment the parent revision and use the
durable profile outbox. Deletion prevents issuance immediately and remains
`deleting` while remote revocations are uncertain; it becomes `retired` after
confirmation. A never-provisioned device can retire immediately. Authorization
is `configs.self.issue` for the owner and `profiles.manage` for administrators
managing another profile. Foreign devices are hidden with 404.

Existing AWG profile identities are adopted into a default Device 1 without
changing remote peers or queued intent payloads. New configuration snapshots use
`node-plane-backend-v3` and include devices, region identities and access sources.
Validated v1/v2 snapshots are converted in memory while keeping the original
checksum for restore confirmation; their grants remain manual. See
[DEVICES_AND_PROVISIONING_PLAN.md](DEVICES_AND_PROVISIONING_PLAN.md) for the
remaining device-aware execution, issuance and interface work.

The execution slice now distinguishes AWG tasks by device UUID without altering
legacy command identities. Operation task representations include nullable
`device_id`; VLESS remains profile-based. Node retirement requires confirmation
for every device peer, and node readiness considers all active devices.

POST `/api/v1/profiles/{profile_id}/config-issuances` accepts optional `device_id`
for AWG and returns the selected device UUID. Omission selects the sole active
device; ambiguity returns `device_required`, and inactive devices return
`device_unavailable`. A foreign device is hidden with `resource_not_found`.
Passing a device to a VLESS request is unsupported. AWG artifacts bind to the
device revision as well as the existing profile/node revisions; peer deletion
and renaming invalidate stale artifacts. AWG labels and filenames include the
device display name.

Traffic collection uses private per-device AWG baselines and the existing
aggregate monthly response. Unknown peer observations never become zero usage,
and admin policy changes reset all peer baselines to exclude the paused period.
These accounting baselines are not configuration snapshot data.

## Automatic AWG ports (2026-10-07)

Node settings accept `awg_port_mode` (`auto` or `manual`). Omitted ports use
automatic preset defaults: QUIC UDP/443, DNS UDP/53, Chaos a random UDP port in
1024–9999. An explicit port with no mode defaults to manual. The target agent
checks UDP listeners and Docker publications, then chooses a bounded fallback
when needed. Routine apply tries the stored port first and does not randomize
an available port. Manual ports are not replaced by a fallback.

Verified runtime results include `awg_port` and a digest of the effective
settings. The controller validates the candidate policy and digest before
persisting the actual port, keeping the original durable command unchanged.
An uncertain deployment or a late bind failure remains blocked for recovery.

## Database deployment revisions

The HTTP factory performs no migrations. `/health/ready` checks the immutable
`backend_schema_revisions` journal, reader compatibility and required tables;
missing, changed or incompatible revisions return 503. Deployment uses
`backend.admin_cli init-schema` before activating the new release. Worker startup
also checks schema compatibility. Migration history survives configuration backup
restore/reset, and rolling back application binaries never downgrades the schema.
See [migration instructions](app/db/MIGRATIONS.md) for revision and rollback rules.
