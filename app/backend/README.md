# Backend application layer

Current stack checkpoint — 2026-10-05: the PTB monolith is removed. Backend,
worker, aiogram client and Rust execution services are the supported stack.
The user reports successful functional testing with PostgreSQL; SSH agent
installation is provisionally accepted from earlier tests rather than a new
clean-host run. Historical checkpoints below do not reopen completed migration
work. Shared update metadata and release-maintenance helpers are retained.

Status: identity/authorization/opaque credentials and initial HTTP API implemented.
The new Telegram adapter is not connected. The systemd installer now initializes
the backend schema and installs separate API and worker units.
Current production bot remains unchanged.
HTTP request handling uses FastAPI's asynchronous server and middleware, but
the current database-backed endpoints are synchronous functions executed in
its thread pool. The finite driver worker and gRPC client are synchronous too.
The future aiogram client will use asynchronous HTTP calls; Python backend I/O
should become async only where concurrency or latency measurements justify it.
The implemented profile/grant/config models are VPN-specific. Future curated
services use the boundaries in
[SERVICE_EXPANSION_ARCHITECTURE.md](../../SERVICE_EXPANSION_ARCHITECTURE.md),
not additional values in the VPN protocol column. Licensing is outside the
current backend scope.

Local administration uses the configured PostgreSQL database. Set
NODE_PLANE_SHARED_DIR to the install's shared directory so config loads its .env.
Do not put a DSN/password or bearer token into command-line arguments.

Accounts and VPN profiles have independent UUIDs. A profile optionally points
to an owner account; it never stores a Telegram ID. Telegram IDs are optional
`backend_external_identities` of accounts, so the same account can later be
used from another authenticated client. Create an account without Telegram,
then optionally attach Telegram as another login:

```bash
PYTHONPATH=app .venv/bin/python -m backend.admin_cli create-account
PYTHONPATH=app .venv/bin/python -m backend.admin_cli link-telegram --account-id ACCOUNT_UUID --telegram-id 123456789
```

The link command is idempotent for the same account and refuses a Telegram ID
already linked to another account. Account-bound credentials work without any
Telegram identity. Future CLI/web login methods must bind to the account UUID,
not create another profile namespace.

From the checkout, using its supported Python runtime:

```bash
PYTHONPATH=app .venv/bin/python -m backend.admin_cli init-schema
PYTHONPATH=app .venv/bin/python -m backend.admin_cli bootstrap-admin --telegram-id 123456789
PYTHONPATH=app .venv/bin/python -m backend.admin_cli issue-token --kind adapter --output /path/to/new-adapter.token
PYTHONPATH=app .venv/bin/python -m backend.admin_cli issue-token --kind account --account-id ACCOUNT_UUID --scope account.self.read --output /path/to/new-account.token
PYTHONPATH=app .venv/bin/python -m backend.admin_cli revoke-token --id CREDENTIAL_ID
```

Provisioning commands are trusted local administrative actions, never API
routes. An adapter credential grants trusted Telegram delegation; store it
where only the adapter's service identity can read it. Account credentials bind
one account and cannot delegate. Expiry defaults to 30 days. Renewal issues a
new token/file; validate new configuration, then revoke the old credential.
The token ID can be printed and audited; the secret must never be logged.

CredentialService.authenticate is the boundary that constructs an authenticated
Principal. Transport code must never construct one from client-supplied JSON.
IdentityService and resolve_actor load current account identity/role/status;
require_permission and require_profile apply resource authorization. An adapter
scope is not an admin role. Administrative config access must be explicit.
Service actors are intentionally rejected by human resolve_actor; their distinct
background-workflow authorization remains to be implemented.

No old-data migration is performed. bootstrap-admin deliberately approves and
promotes exactly the specified Telegram identity. New registration remains a
pending member. New tables are created only by init-schema, not during imports.

Tests use SQLite for isolated SQL/policy checks. PostgreSQL integration, including
concurrent identity resolution, is still required before production cutover.

## Run the initial HTTP service

For a systemd installation, `scripts/install.sh --mode simple --install-systemd`
initializes the backend schema in the shared PostgreSQL database and installs
`node-plane-backend.service` plus `node-plane-backend-worker.timer`. The API
binds to `127.0.0.1:8080`; the timer runs the finite worker every five seconds
after its previous invocation finishes. Both use the active release symlink
and the same shared environment file as the legacy bot. `scripts/update.sh`
reinitializes the schema and restarts the API when switching releases.
`scripts/healthcheck.sh --mode simple` checks both units and API readiness.
Portable Docker installation is temporarily unsupported during this migration.
The installed legacy Telegram bot still uses its old business stack; installing
these services does not switch it to the new backend.

Install requirements.txt and requirements-dev.txt for HTTP testing. Backend
transport dependencies are also listed separately in requirements-backend.txt.
Initialize schema and credentials with the local commands above, then run:

```bash
PYTHONPATH=app .venv/bin/python -m uvicorn backend.http_api:application --factory --host 127.0.0.1 --port 8080 --no-access-log
```

This factory does not create or migrate tables on startup. /health/live reports
process liveness; /health/ready checks the identity slice's required tables.
Readiness does not yet imply driver/agent reachability or full application readiness.

Implemented routes:
- POST /api/v1/integrations/telegram/identities/resolve: adapter credential,
  UUID Idempotency-Key, strict body containing only telegram_user_id.
- GET /api/v1/me: account token, or delegated adapter token with
  X-Node-Plane-Telegram-User-ID. Role and effective permissions come from backend.
- GET /openapi.json: generated schema; backend-openapi.json is its checked-in
  snapshot. No deployment credentials are embedded.

All responses have a backend-generated X-Request-ID and Cache-Control: no-store.
Validation errors do not echo submitted values. Duplicate authorization or actor
headers are rejected. Identity registration stores command identity and account
link atomically; replay cannot change the target user. This journal is specific
to registration and is not the executor for future node/config operations.

Token header contents must be injected by the API client, not placed in a URL
or a shell command. Public binding/TLS and production systemd integration will
be addressed during the deployment stage.

## Profile and available-node reads

The admin-only `GET /api/v1/profiles` lists all profiles. An authorized caller
can read a profile's current grants at `GET /api/v1/profiles/{id}/grants`.
The aiogram client uses these reads before revision-checked access changes;
the separate member view remains filtered by owner and active grants.

The clean target schema adds backend_profiles (UUID, runtime identity, owner,
state/revision), backend_nodes (public node metadata and supported protocols)
and backend_grants. No automatic mirror/import of old profiles/servers is made.
Run init-schema again to create these development tables. Trusted local creation:

```bash
PYTHONPATH=app .venv/bin/python -m backend.admin_cli create-profile --runtime-name alice --display-name Alice --owner-account-id ACCOUNT_UUID
```

GET /api/v1/me/profiles returns only owned profiles with keyset pagination.
GET /api/v1/profiles/{UUID} allows own reads or explicitly authorized admin reads.
GET /api/v1/me/profiles/{UUID}/summary is owner-only. It reports profile
creation/expiry, grouped node grants, and successful config issuance activity;
it does not infer live VPN traffic from stored records. Existing profiles
without a recorded creation date return null for that field.
GET /api/v1/me/nodes lists only enabled nodes with active owned grants and supported
protocols; frozen/expired profiles do not contribute grants. It returns no SSH
parameters or credentials. Pagination filters ownership before applying limit.
OpenAPI defines safe output models for these routes.

GET /api/v1/access-requests supports keyset pagination and case-folded search
over linked Telegram ID, username, first/last name, or account ID. The admin
detail route GET /api/v1/access-requests/{UUID} returns only pending requests;
decisions continue through the idempotent POST endpoint.

These read models are not connected to legacy driver provisioning tables. Local profile
creation does not install runtime or grant VPN access. New state is exercised in
isolated tests; PostgreSQL and real driver integration remain pending.

## Administrator node inventory

`GET /api/v1/nodes` and `GET /api/v1/nodes/{key}` expose the new backend-owned
inventory to approved administrators. `POST /api/v1/nodes` creates a disabled
draft with public metadata, supported VPN protocols/transports, and a small
allowlist of desired public protocol settings. `PATCH /api/v1/nodes/{key}` edits
that desired record. Both writes require a UUID `Idempotency-Key`; PATCH also
requires the current `ETag` in `If-Match`. The response carries separate
`desired_revision` and `applied_revision`. A new draft starts at 1/0, and edits
advance only the desired revision. Settings are replaced as a whole when sent
in PATCH. They cannot be mistaken for runtime-confirmed values.

Node keys cannot be reused after retirement. A protocol with active grants
cannot be removed from the inventory. Creation does not deploy an agent or
protocol runtime. The admin model deliberately has
no SSH credentials or private keys. The current `GET /me/nodes` remains a
separate grant-filtered member view. Xray config issuance now compares applied
node settings and successful profile intents before returning credentials.
`init-schema` creates the node command journal on a
fresh development database; older prototype schemas need recreation.

For a node with an already installed agent and configured driver target, admin
`POST /api/v1/nodes/{key}/apply-settings` queues the current desired revision
(`If-Match` plus UUID `Idempotency-Key`). Poll
`GET /api/v1/node-settings-operations/{id}`. The shared backend worker sends a
dedicated revision-bound command to the driver; the agent journals it under
the same lock as profile intents. It updates node.env, opens declared ports,
deploys selected protocol containers, then verifies effective config fields
and running containers. Only the matching confirmed result advances
`applied_revision` and enables the node. Incomplete drafts are rejected.
After creating a disabled backend draft, install/bind its agent with the
explicit installer mode. SSH credentials are CLI-only and never stored in the
public node inventory or read from the legacy bot registry:

```sh
NODE_PLANE_BIN_SOURCE=release scripts/setup_driver_agents.sh --backend-node-key lv1 --backend-ssh-target root@lv1.example --backend-ssh-identity /path/to/admin-key
NODE_PLANE_BIN_SOURCE=release scripts/setup_driver_agents.sh --backend-node-key local1 --backend-local
```

Use `--backend-ssh-port` for a non-default SSH port and
`--backend-agent-host` if the driver's reachable agent address differs from
the SSH address. `--dry-run` checks inputs, binaries and SSH reachability
without installation. The installer preserves other `NODE_AGENT_TARGETS`
entries, restarts driver after updating the shared environment, and verifies
the new agent route. The binary assets must match this checkout's gRPC schema.
The new Telegram client can queue this same single-node rollout through
`POST /api/v1/nodes/{key}/agent-rollouts` and inspect its status with
`GET /api/v1/agent-rollouts/{task_id}`. An approved administrator supplies a
local or SSH target, but never an SSH key path through the API. The worker
uses the controller's configured `SSH_KEY` (or default OpenSSH identity) and
release binaries, then verifies
the driver route. The request has a durable idempotency key. If the worker is
interrupted mid-installation, the task becomes `blocked` rather than silently
running the installer a second time. Inspect the controller and node before
requesting a fresh rollout. A successful rollout only prepares the agent;
the separate Apply settings command installs the selected VPN protocols.

The authenticated maintenance API exposes the same guarded lifecycle to
approved administrators. `GET /api/v1/nodes/{key}/maintenance` shows the
verification target, drain and cleanup phases. Before full cleanup,
`POST /api/v1/nodes/{key}/bind-verification-target` checks an active agent and
binds a local host or `root@host` on port 22, using the controller's `SSH_KEY`
for SSH preflight. The host key must already be pinned in known_hosts.
`POST /api/v1/nodes/{key}/drain` refuses an unbound target and queues profile
revocations. Once they are complete, each
`POST /api/v1/nodes/{key}/cleanup-step` advances one durable phase under the
worker lock. Its `expected_phase` body must equal the phase shown before the
request; a retry after a lost response returns the reached phase without
advancing again. `POST /api/v1/nodes/{key}/verify-and-retire` checks that the agent,
runtime and standard artifacts are absent before removing the node record.
Remote final verification requires `NODE_PLANE_REMOVAL_SSH_KEY`, an independent
root SSH key distinct from `SSH_KEY`, and a pinned known_hosts entry. Set
`NODE_PLANE_BOT_PUBLIC_KEY_FILE` to the bot SSH public key file, or the API
uses `SSH_KEY.pub`. Local final verification needs the public key but no second
private key. Every remote check fails closed if it cannot verify the host.
`POST /api/v1/nodes/{key}/retire-registry-only` requires explicit acceptance
that remote artifacts may remain; it is for a lost or expired VPS and never
reports a verified full cleanup. These HTTP mutations enforce the same file
lock as the worker and trusted local CLI.
The worker first performs a read-only agent probe. If runtime files or protocol
configs are missing, the driver prepares this unmanaged agent: it copies the
versioned runtime bundle, preserves any existing node.env, and installs Docker.
Only after preparation succeeds does the settings command start. Inside that
durable command the agent initializes missing Xray/AWG configs without replacing
existing keys, deploys protocols, and verifies the result. An absent agent or
failed preparation leaves the task queued without starting that command.
An unknown result stays blocked; the worker attempts read-only recovery from
the agent journal on restart. Never resend it with a new command ID while
the old outcome remains uncertain. For a permanently interrupted command,
restart the node agent first, then retire that exact command through the
trusted local CLI while holding the worker lock:

```sh
PYTHONPATH=app .venv/bin/python -m backend.admin_cli resolve-blocked-node-settings --task-id TASK_UUID --admin-account-id ACCOUNT_UUID --lock-file /opt/node-plane/shared/backend-worker.lock
PYTHONPATH=app .venv/bin/python -m backend.executor --lock-file /opt/node-plane/shared/backend-worker.lock
```

The agent inspects live config and containers, marks the old command
superseded, and never claims it succeeded. Backend queues a fresh revision;
only its verified result enables the node. If the agent is still running the
old command, restart is required before this repair. The old Telegram bot and
installer do not use this path yet. PostgreSQL integration and real-node
testing still require work.

`GET /api/v1/nodes/{key}/runtime` uses a dedicated read-only driver RPC to
inspect the configured agent directly. It returns health state, runtime
version/commit, and whether Xray/AWG config files exist. The driver verifies
the agent's reported node key and never reads the legacy `servers` table for
this call. An unconfigured or unreachable agent produces a typed, redacted
error. `settings_verified` remains false: this read-only observation reports
file presence and health, not a fresh comparison with desired settings. The
legacy `ApplyNodeSettings` RPC still reads `servers` and is not used by the
backend settings worker.

## Account access requests

The backend now owns access-request creation, pending-list reads, and admin
approve/reject decisions. `POST /api/v1/me/access-requests` requires an
Idempotency-Key UUID. `GET /api/v1/me/access-requests` lists the caller's own
history; `GET /api/v1/access-requests` lists pending requests for approved
administrators. `POST /api/v1/access-requests/{id}/decision` accepts only
`approve` or `reject` with an Idempotency-Key. Replaying the same decision key
returns its saved result; another decision on a closed request returns a conflict.
One account can have only one pending request, and a rejected account may apply
again. The request transition and account status change share one transaction.
Approval does not create a VPN profile or grant access to any node.

`GET /api/v1/system/access-requests` returns the request-enabled flag and the
message displayed to accounts without access. `PATCH` updates either field and
requires administrator settings permission. Request creation checks the same
backend policy transactionally, so hiding the Telegram button is not the only
enforcement layer. The policy response also reports a per-administrator
`notify_requests` preference; administrators can update it independently, and
the Telegram adapter checks it before sending request notifications.

`GET /api/v1/system/bot-title` returns the public menu title; administrators
change it with `PATCH` and a `title` value. The setting belongs to the backend
so non-Telegram clients can use the same presentation metadata.

These routes use the backend identity tables. Telegram identity resolution also
stores the user's current display name, username, and Telegram language code;
pending-request and admin-account reads include those fields for the adapter.
`GET /api/v1/me` returns the saved `locale` and whether the user has explicitly
selected one, and
`PATCH /api/v1/me/preferences` accepts `{"locale":"ru"}` or
`{"locale":"en"}`. The client chooses a default locale from Telegram on first
resolution, asks the user to confirm a first-run choice, and persists the
selection. Initialize the schema with
`admin_cli init-schema` before starting the HTTP service.

## Administrator account management

Approved administrators can list accounts at `GET /api/v1/accounts`, inspect one
at `GET /api/v1/accounts/{id}`, and change its role/status with
`PATCH /api/v1/accounts/{id}`. The detail response carries an ETag; PATCH
requires that revision in `If-Match` and a UUID `Idempotency-Key`. A repeated
command returns the saved response, a changed payload with the same key
conflicts, and an outdated revision fails before mutation. The account list
includes verified Telegram ID but no username-based identity matching.

The update serializes administrator changes through one database guard row,
checks the actor's current role inside the transaction, and refuses removal
of the last approved administrator or self-revocation. A pending access request
must be decided through its own endpoint before account state changes.
Approving an account does not assign a VPN profile or node grant. The current
installer and Telegram bot do not use these routes yet. `init-schema` creates
the command journal; the experimental account revision column requires a fresh
development database rather than an in-place migration of earlier prototypes.

Before draining a node, independently reach its active agent and bind the
verification target. The preflight checks `node_key` in the agent config and
stores a hash of the host's machine ID. It refuses an unreachable agent or
a different node at the supplied address:

```sh
PYTHONPATH=app .venv/bin/python -m backend.admin_cli bind-node-verification-target --node-key NODE --admin-account-id ACCOUNT_UUID --lock-file /opt/node-plane/shared/backend-worker.lock --ssh-target root@node.example --ssh-identity-file /path/to/admin-key
```

Use `--local` for the bot host instead of `--ssh-target`. The first
node-retirement phase is available to a trusted local administrator:

```sh
PYTHONPATH=app .venv/bin/python -m backend.admin_cli drain-node --node-key NODE --admin-account-id ACCOUNT_UUID --lock-file /opt/node-plane/shared/backend-worker.lock
PYTHONPATH=app .venv/bin/python -m backend.executor --lock-file /opt/node-plane/shared/backend-worker.lock
PYTHONPATH=app .venv/bin/python -m backend.admin_cli node-drain-status --node-key NODE --admin-account-id ACCOUNT_UUID
```

Use the same stable worker lock for drain and executor. Drain atomically disables
the node for new grants, removes its grants and queues delete intents for every
profile that ever targeted it. Old queued ensures are superseded before any RPC.
Repeating drain returns the original operation IDs. A blocked or pending task
keeps `revocations_complete` false. Drain itself does **not** delete the node
record, runtime, credentials or agent. Do not run legacy FullCleanupNode against
a fenced node.

After all revocations are confirmed, an approved administrator can advance
backend-owned cleanup one phase at a time, using the same lock file:

```sh
PYTHONPATH=app .venv/bin/python -m backend.admin_cli cleanup-node-step --node-key NODE --admin-account-id ACCOUNT_UUID --lock-file /opt/node-plane/shared/backend-worker.lock
```

The three calls prepare a durable agent mutation fence, remove runtime/configs
with agent verification, and schedule agent uninstall. The transient uninstall
unit also removes the exact bot SSH public key from the agent's home and
standard root/user homes after stopping the agent; a scheduling
failure leaves that key available for recovery. The agent refuses preparation
while any managed profile's latest intent is not a successful delete or any
command is unfinished. The command UUID is persistent across retries. Prepare
and runtime deletion are safe to retry after a timeout. Before uninstall RPC,
backend commits
`uninstall_uncertain`; a timeout then requires independent verification and is
never retried automatically. A returned `uninstall_scheduled` only means the
transient systemd removal was scheduled, not that it completed. The node record
is deliberately retained in both states.

After uninstall, independently inspect the bound host and then remove the node
record. Remote verification requires a separate root SSH identity with a pinned
known_hosts entry; the bot's SSH key should have been deleted by then:

```sh
PYTHONPATH=app .venv/bin/python -m backend.admin_cli verify-and-remove-node --node-key NODE --admin-account-id ACCOUNT_UUID --lock-file /opt/node-plane/shared/backend-worker.lock --bot-public-key-file /path/to/bot-key.pub --ssh-target root@node.example --ssh-identity-file /path/to/admin-key
```

For a local node, use `--local` instead of the SSH arguments. Verification
checks the inactive agent service, absence of its standard unit, binary,
configuration, runtime, state and log paths, bot SSH key and default managed
containers. The host check is read-only and rejects a changed machine ID.
The immutable retirement record stores the method, target and host fingerprint.
This identifies the configured agent host before cleanup, although cloned
machine IDs and independent root compromise remain outside this guarantee.
Custom artifacts, SSH homes
outside `/root` and `/home`, and old untracked UFW rules are not proven absent
by this check and require separate review.

When the VPS cannot be checked, an administrator can explicitly remove only
the backend record and abandon all pending work for that node:

```sh
PYTHONPATH=app .venv/bin/python -m backend.admin_cli remove-node-registry-only --node-key NODE --admin-account-id ACCOUNT_UUID --lock-file /opt/node-plane/shared/backend-worker.lock --reason 'VPS expired and agent is unreachable' --accept-unverified-runtime
```

This does not claim that remote users, containers or files were removed. A
permanent tombstone records the reason and prevents historical profile intents
from addressing that node key again. Neither path has been tested on a real
node or PostgreSQL yet.

## Profile desired-state commands

GET /api/v1/admin/overview returns database-backed administration counts and
nodes with blocked tasks. It does not probe agent reachability or compare live
runtime versions; the Telegram Status screen labels that limit explicitly.
GET /api/v1/nodes accepts an optional `search` term over node key, title and
region; the search term is bound to its cursor so a cursor from another search
cannot be reused accidentally.

GET /api/v1/system/updates reports the current update track and last check.
PATCH /api/v1/system/updates/preferences changes branch, dev track or automatic
checks for administrators. A track change clears the old check result; the next
update must be checked against the newly selected track. The release-cleanup
GET/POST routes use the existing simple-mode cleanup service.

POST /api/v1/profiles requires profiles.manage and creates a UUID profile with
an autogenerated immutable runtime name. Input is display_name, optional
owner_account_id, and an optional initial grant list; profile, grants, and
provisioning intent commit together. PATCH /profiles/{id} edits display_name, frozen, expires_at;
PATCH /profiles/{id}/grants replaces the whole grant list, requiring grants.manage.
DELETE /profiles/{id} requires profiles.manage and marks the profile for
deletion: grants are removed, access is frozen, and historical node targets get
durable delete intents. The admin list retains pending or blocked deletions and
hides them only when the delete operation succeeds. The tombstone and operation
history remain in the database for audit and recovery.
Ownership and runtime_name are not editable through these routes.

All commands require UUID Idempotency-Key. Edits/grants require If-Match with
the quoted desired revision, e.g. "1"; GET profile returns ETag. Successful
changes atomically increment revision, save desired state and retain the result
in backend_profile_commands. Replay checks current permissions and returns the
original result without another update; key/payload conflict returns 409.
Missing revision returns 428 and outdated revision returns 412. Expiry requires
a timezone and is stored in UTC. Invalid grant targets fail without deleting the
previous grant list.

Responses contain operation_id and runtime_status=awaiting_executor when there
are runtime targets, or no_targets when there are none. HTTP commands do not invoke the executor directly; enforcement occurs only after
the separate worker successfully applies the saved intent. They do not replace current bot provisioning.
Run init-schema again to create the profile command journal and outbox during development.

## Durable operations and runtime intents

Every profile command saves desired state, its replay response, an operation and
per-node/protocol intent snapshots in one transaction. The outbox includes both
current grants and historical targets, so removing a grant cannot lose the task
needed to revoke it. Frozen or already expired profiles produce delete intents;
active grants produce ensure intents. Stable task UUIDs are reserved for driver
idempotency. The executor must process revisions in order or supersede obsolete
intents safely, serialize conflicting work on each node, and reconcile uncertain
outcomes before retrying. Expiry scheduling is implemented by the worker,
which queues profile revocations while retaining grants and identities.

GET /api/v1/operations/{id} requires operations.read. The initiating actor can
inspect it; another actor additionally requires profiles.manage. Responses expose
status and task targets/actions, never internal runtime names, intent snapshots
or VPN secrets. HTTP stores durable work only: no background worker is started by the API and
no 202 response claims runtime success. The separate executor records its result. PostgreSQL integration and reconciliation of uncertain outcomes
remain acceptance requirements.

## Explicit driver executor (development)

### Xray and AWG config issuance

`POST /api/v1/profiles/{profile_id}/config-issuances` queues an Xray TCP/XHTTP
link or an AWG `.vpn`/`.conf` artifact for a granted node; it requires a UUID
`Idempotency-Key`. Poll
`GET /api/v1/config-issuances/{id}`, then fetch the short-lived link from
`GET /api/v1/config-issuances/{id}/artifact`. The worker checks that the
profile's current revision was applied and that the node settings are current,
then verifies the live protocol user through the driver. Xray reads public
connection parameters from the agent. AWG refreshes the stored private client
config against the current server config and verifies that its peer still exists.
The artifact is assembled only when the
artifact is requested, after repeating access, revision, and live checks.
Issuances expire after 15 minutes; revocation invalidates them immediately.
The issuance table stores only public Xray metadata or an AWG artifact digest,
not the generated artifact. The successful AWG profile intent still retains
its private client config in the backend task result. Real-node acceptance
remains to be implemented.

Clients may request `?wait=true` to perform the read-only issuance checks in the
API thread pool immediately, without waiting for the worker timer. The durable
issuance is claimed atomically; a concurrent worker cannot run the same check.
Access, revision, and live-state checks still run again at artifact download.
The AWG artifact response also includes a `files` array containing both `.vpn`
and `.conf`, assembled from the same live refresh. The Telegram client embeds
these files, a collapsed QR block, and a monospace URI in its control message.
VLESS uses the same layout after selecting TCP or XHTTP. If rich media is not
supported by the Telegram deployment, downloads and the QR are sent separately.

Driver ProvisioningService.ApplyProfileIntent takes a single node/protocol,
ensure/delete action, runtime name, desired revision and explicit Xray identity.
It does not read or write legacy profile business tables. The command identity
header is mandatory; replay returns the existing driver operation, and a changed
payload with the same identity is rejected. AWG result payloads are retained
privately for future config issuance. The backend preserves each profile's Xray
UUID/short ID across revoke and regrant. A failed agent call is treated as an
unknown outcome, without exposing the raw transport error.

After initializing a **fresh development database** and starting the rebuilt
driver, run a finite drain of the queue:

```sh
PYTHONPATH=app .venv/bin/python -m backend.executor --lock-file /opt/node-plane/shared/backend-worker.lock
```

Use exactly the same lock path for every invocation. This implementation supports
one worker host, not distributed workers. It commits each task claim before RPC,
uses its UUID as the driver command identity, and stores private driver results.
On startup any running task becomes blocked; later tasks on that node remain
blocked from dispatch until explicit reconciliation is implemented. Completed
tasks are not executed again. A pending ensure whose snapshot has expired is
also blocked instead of granting expired access. The CLI does not start a daemon,
and is not yet wired into install.sh/systemd or the Telegram bot.

This changes the intermediate backend table constraints/columns. init-schema is
not an upgrade migration for the preceding experimental schema: use a fresh test
database for this stage. No existing installation is reset automatically. Legacy
bot tables and running nodes are untouched. Real PostgreSQL/driver/agent execution,
reconciliation of blocked tasks, scheduled expiry enforcement, and AWG config issuance
remain pending. The worker's HTTP operation status can now become running,
succeeded or blocked in addition to awaiting_executor/no_targets.

## Read-only recovery from the driver journal

OperationService.GetOperationByCommand looks up a persisted command identity
without starting execution. On each worker invocation, after marking abandoned
running tasks blocked, reconcile_completed queries blocked task IDs. A confirmed
SUCCEEDED result for ApplyProfileIntent on the matching node/runtime profile
restores the task and its private result payload and recalculates the operation
status. Later revisions can then proceed. No mutation RPC is called during this
lookup and no new command identity is generated.

A missing, running, failed or mismatched driver record, and any lookup exception,
leave the node blocked. This is journal recovery, not full runtime reconciliation.
Agent ListRemoteProfiles currently inspects saved configuration files and cannot
prove live AWG peers/Xray API state, so it is deliberately not used to declare an
uncertain mutation successful. Failed/interrupted commands still require a
stronger live-state inspection and a deliberate repair contract.

## Live profile inspection

ProvisioningService.InspectProfileIntent is a read-only route to the agent's
InspectProfile RPC. The runtime helper holds the same config lock used by profile
mutations. Xray checks both TCP and XHTTP through HandlerService inbounduser,
including UUID and flow, and compares them to the durable config. AWG runs
`wg show <interface> dump` inside its container and compares public key, PSK and
AllowedIPs against the stored peer. A revoked peer archive identifies an AWG
peer after removal. Without that identity, absence cannot be proved and inspection
fails as unavailable. Missing configs/API, malformed data and container failures
are never treated as a confirmed absent profile.

Only disk_present/live_present/identity_matches/config_available booleans cross
the inspection RPC; no client config, PSK or UUID is returned. The worker stores
observations for blocked tasks and their timestamp. Operation task responses now
include optional inspection and inspected_at. A failed subsequent inspection
replaces previous evidence with available=false rather than leaving stale success.

Inspection does not unlock interrupted/failed work: a late in-flight RPC could
still change state after the observation. Only confirmed success in the driver
journal unlocks automatically. A fenced repair protocol that establishes old
execution has stopped is still required for full reconciliation. Helpers and new
agent/driver contracts require updated runtime assets and both rebuilt binaries.
Tests exercise mock live responses and actual localhost gRPC transport; tests on
real protocol containers and PostgreSQL remain pending. The development outbox
schema now adds inspection_json/inspected_at; use a fresh test database.

## Agent revision fences and durable completion

New backend ApplyProfileIntent execution now goes through the agent's explicit
RPC rather than legacy Add/Delete RPCs. The runtime helper persists a command ID,
full-payload fingerprint and revision fence per runtime profile/protocol in
/etc/node-plane/profile-intents.sqlite3. A node-wide flock serializes these
commands; the runtime child inherits the lock descriptor. The fence and running
entry commit before any mutation. Duplicate successful commands return their
saved result, including the AWG payload. Payload conflicts and lower/equal
revisions with another command ID are rejected before execution.

Any unfinished or failed external mutation leaves its running entry intact and
blocks further explicit mutations across the node. This survives process restart;
no retry or newer revision silently bypasses it. A command that successfully
finished after the driver timed out can be recovered through read-only
RecoverProfileIntent RPCs: the agent verifies the entire intent fingerprint and
returns only a durably succeeded result. Backend recovery falls back to this
agent journal when the matching driver result is unsuccessful or missing.

The fence covers the new explicit backend path. Existing legacy provisioning
RPCs, node maintenance and direct operator edits are not part of this journal;
coordinating them with the new worker remains an integration requirement. Full
reconciliation of genuinely interrupted/failed commands still requires operator
inspection or an explicit audited repair/reset contract. Runtime cleanup preserves
fences; final agent uninstall first stops the agent and its children, then removes
its intent journal and lock along with the other owned files. Helper/runtime and
both binaries must be updated together. No release was published by this change.

## Transition guard for legacy mutation RPCs

The updated agent routes legacy Xray/AWG Add/Delete user scripts through the
same durable node-wide lock as ApplyProfileIntent. Before launching the old
script it checks whether its protocol/runtime profile has a revision fence.
Owned profiles are rejected; unrelated legacy profiles remain usable. The
check and child execution share the lock, closing the check-then-mutate race.
An unreadable journal or a missing helper with a journal fails closed.

Legacy init/deploy/apply-settings/sync-xray/entropy-regeneration calls are
rejected when any profile is fenced. Direct SyncNodeEnv, DeleteRuntime, and
writes of active Xray/AWG configs through SyncRuntimeFiles also use a held
maintenance guard. Other packaged runtime code/assets can still be updated.
Final UninstallAgent refuses fenced nodes and writes a durable removal marker
before scheduling the delayed service stop, preventing new explicit mutations
in that gap. The marker is removed only after the service and children stop.
A failed uninstall schedule leaves a fail-closed marker for manual recovery.

This is a transition guard, not a replacement for backend-owned node maintenance.
A node with fenced profiles must be decommissioned through that future path;
legacy full cleanup now refuses it instead of silently discarding the journal.
Direct root/operator changes to Docker, config files, or the journal remain
outside this RPC boundary. A controlled repair/decommission contract for
actually interrupted commands is still pending. Existing deployments are not
changed until rebuilt driver/agent and runtime assets are installed together.

## Deliberate repair of interrupted work

If a blocked task is not confirmed successful in either journal, stop at the
block. Restart node-plane-agent.service on that node to stop its previous
systemd process group. Do not delete the agent journal or call the old profile
RPCs. The new service instance has a fresh instance ID. Then inspect the task
and run a trusted local repair on the bot host, using the same lock path as the
worker and an approved backend administrator account:

```sh
PYTHONPATH=app .venv/bin/python -m backend.admin_cli resolve-blocked \
  --task-id TASK_UUID --admin-account-id ADMIN_ACCOUNT_UUID \
  --lock-file /opt/node-plane/shared/backend-worker.lock
PYTHONPATH=app .venv/bin/python -m backend.executor \
  --lock-file /opt/node-plane/shared/backend-worker.lock
```

ResolveProfileIntent checks the full original payload and requires the agent
instance to differ from the one that began the blocked command. Under the
node-wide lock it performs a fresh live inspection, saves an audit observation,
and marks the old command superseded without replaying it. If inspection is
unavailable or the agent has not restarted, repair stops with the fence intact.
The backend then marks the old blocked and queued tasks for that profile/node
superseded, increments the profile revision and queues current desired state.
The old command ID remains rejected forever. A failure between the remote audit
and the backend commit is retried with the same ID; the agent returns its saved
audit record. Backend repair results are journaled in backend_repairs and a
second invocation returns the existing new operation.

This is local, administrator-only maintenance. It is not exposed to Telegram or
public HTTP. The repair requires a systemd restart that quiesces the old agent
process group; direct/root actions outside that group or delayed Docker daemon
work are not fully covered by this contract. Real-node validation must verify
that old protocol commands cannot complete after restart. If AWG has no saved
peer identity, live absence cannot be proven and repair remains blocked. Backend
node decommissioning and coordinated protocol settings updates still need their
own API; legacy full cleanup intentionally refuses fenced nodes.
# Node installation and maintenance jobs

## System updates

The version catalog is available at `GET /api/v1/system/updates/versions?offset=0`.
`GET /api/v1/system/updates/rollout` reads the running driver/agent binary commits
and runtime commits through the native driver, using the backend node registry.
Unknown reachability is not evidence that a component is current.

`POST /api/v1/system/updates/run` requires `Idempotency-Key` and accepts either
`{"kind":"version","branch":"dev","target_ref":"v0.4.3-alpha.19"}`,
`{"kind":"agents"}` or `{"kind":"runtimes"}`. The version/ref is checked
against the selected branch catalog and downgrade policy. Dev HEAD execution is
pinned to its selected commit. `GET /api/v1/system/updates/jobs/{id}` reports
worker progress and individual batch outcomes. The worker uses the systemd
updater for stack changes and existing rollout/native runtime jobs for nodes.

The database stores a claim before launching a system update or driver-only
installer. Interrupted launches are blocked, never automatically replayed.
Agent/runtime child commands use deterministic IDs, so restarting batch
coordination cannot create duplicate child jobs. Successful installer exits
are followed by a live commit check. Batch errors are reported per component;
unknown nodes can be repaired through their node cards.

Automatic upstream checks run from the existing backend-worker timer, at most
hourly when enabled. They never install updates automatically. These endpoints
support the systemd installation only; Docker stack installation remains
unsupported.

`POST /api/v1/nodes/{key}/actions` accepts an action, desired revision and
`Idempotency-Key`; `GET /api/v1/node-jobs/{id}` reports durable progress. The worker
executes native driver/agent RPCs, not Telegram-owned shell commands. Bootstrap,
reinstall, Docker setup, ports, runtime sync, repair and entropy operations share
this contract. `GET /api/v1/nodes/{key}/services` supplies live installation and
runtime facts for conditional UI actions.

Config-changing jobs disable issuance until their node settings are acknowledged
and profile reconciliation completes. Clean reinstall invalidates artifact cache
and advances profile intent revisions. Interrupted jobs become blocked; resolve
reads the native journal, or retires an unfinished command after agent restart,
without replaying an uncertain mutation.

`POST /api/v1/nodes/{key}/remove-step` queues full worker-driven removal;
`GET /api/v1/nodes/{key}/removal` reports progress. An explicit `retry: true` retries
a blocked saga. Full removal revokes access before runtime/agent cleanup and only
retires the registry after independent host verification. For SSH nodes configure
`NODE_PLANE_REMOVAL_SSH_KEY` with root access, distinct from `SSH_KEY`; supply the
bot public key via `SSH_KEY` or `NODE_PLANE_BOT_PUBLIC_KEY_FILE`. Verification
credentials are checked before destructive steps. Local verification uses that
public key without an independent SSH connection. Runtime-only cleanup preserves
the node and agent and must not be reported as full removal.

## Backend backups and database support

PostgreSQL is the only supported backend database. The installer initializes
the current PostgreSQL schema directly; legacy database migration commands and
fallback database adapters have been removed. Node agents retain their independent
SQLite command journals for idempotency and revision fencing. Isolated tests may
use SQL fakes; these do not represent supported production databases.

The Backups screen uses `/api/v1/system/backups`: overview, paginated catalog,
metadata, scheduling preferences and durable create/restore jobs. Native snapshots
are private JSON files in `shared/backups/backend`, with checksums and schema
compatibility checks. They contain accounts, external identities, profiles, grants,
node settings/connections and access requests. Bearer credentials, operation
history and issued artifacts are excluded. SSH connection settings can contain
sensitive values, so snapshot files must remain private.

Restore blocks new HTTP mutations and config issuance, creates a pre-restore
snapshot, freezes current profiles and waits for worker-owned revocations. A failed
revocation blocks restoration and leaves profiles frozen; resolve the failed
operations before retrying. The database replacement is transactional, preserves
the restoring administrator, revokes account credentials and clears operational
caches. Restored nodes are disabled and unapplied; enable them and apply their
settings explicitly before issuing fresh configurations. Revisions advance beyond
the current state so agent fences cannot reject the restored configuration.

This is a configuration backup, not a host/filesystem or PostgreSQL disaster
recovery backup. PostgreSQL snapshot isolation and locking still require deployment
integration testing; isolated unit tests verify orchestration and policy only.

## Announcement delivery

`POST /api/v1/announcements/preview` validates text and counts eligible recipients.
`POST /api/v1/announcements` requires an administrator and Idempotency-Key; it
snapshots approved Telegram accounts except the sender. GET on the collection
returns the latest job, and GET on an ID returns durable delivery counts.

A trusted Telegram adapter with `settings.manage` scope uses the integration
claim/ack endpoints without a delegated user header. These endpoints deliver
already admitted work; account/service credentials cannot claim it. Each claim
uses a stable command UUID, is bound to its adapter credential, rechecks account
eligibility, and expires after two minutes. An expired claim becomes unknown,
not queued. No ambiguous send is automatically replayed. The Telegram client
acknowledges explicit failure, success or uncertainty without persisting raw API
errors. Telegram delivery is not exactly-once; unknown outcomes require human
review. The client polls asynchronously and uses Rich Messages with a safe plain
fallback after explicit rejection.

`PATCH /api/v1/me/preferences` also accepts strict `announcement_silent` boolean;
this preference belongs to the backend account rather than its Telegram identity.
A locale preference still requires a linked Telegram identity. Pending broadcasts
block backup restoration; the outbox is excluded from snapshots and cleared
during restoration.

## Alert monitoring

GET `/api/v1/system/alerts` returns policy, current recorded conditions, last scan
status and delivery counts. PATCH on `/preferences` accepts strict enable/resolved
booleans and a 5/15-minute interval. Monitoring is disabled by default. The backend
worker scans after processing queued mutations, with up to four read-only agent
requests in parallel and a five-second per-RPC timeout. Scans are skipped during
backup restoration. Disabled/unapplied nodes and active maintenance are excluded.

Agent runtime inspection reports host measurements in `host_metrics` and an
explicit `inspection_available` marker. It reads `/proc/meminfo`, the runtime
filesystem and host load. The first thresholds are fixed: disk free <10%, used RAM
>=90%, load1/CPU count >=2. Missing, invalid or older-runtime readings are unknown,
and do not clear previous conditions. The driver is checked before and after a
scan; driver failure records a failed scan without manufacturing node outages.
Synchronize node runtimes to obtain these new measurements.

`backend_alert_state` persists active conditions. Transitions produce immutable
alert events and per-admin deliveries; repeated scans do not repeat notifications.
Recovery notices are optional. Removed protocols retire service conditions, and
node retirement deletes its alert state/events/deliveries. Monitoring reads state
and never mutates protocol settings or enables a node.

A trusted Telegram adapter claims and acknowledges deliveries through integration
endpoints, under the same adapter-only `settings.manage` transport scope as
announcements. Each claim is bound to an adapter and command key, expires after
two minutes and becomes unknown rather than being replayed. Recipient admin role
and Telegram identity are rechecked. Alert and announcement transports alternate
inside the async client poller, which survives leaving UI screens. Notification
text belongs to the RU/EN presentation catalog. Snapshot restoration clears
monitoring/outbox state and is blocked while an alert delivery is in flight.

Automated tests use isolated SQL fixtures and fake driver observations. Real
PostgreSQL locking, VPS resource collection and live Telegram delivery still
require integration acceptance.


## Administrator-controlled traffic statistics

`GET /api/v1/system/traffic` and `PATCH /api/v1/system/traffic/preferences`
require `settings.manage`. Collection defaults off. One installation-wide
switch enables accounting for all eligible profiles, including ownerless admin
profiles; member consent is no longer accepted by `/me/preferences`. `/me`
reports `traffic_available`. Schema initialization removes obsolete consent
preferences. Global pauses preserve totals but clear baselines; resuming excludes
traffic from the paused period. Repeating the same policy does not reset totals.

The worker samples native driver/agent byte counters every five minutes, with
batches of 32 pairs and four concurrent requests. Active grants, synced profiles,
applied nodes and approved owners (where present) are required. Maintenance,
restore, drains and cleanup exclude collection. A final transaction rechecks the
global policy generation and resource revisions before recording observations;
an off/on race discards the in-flight batch. Counters are never reset by reads.

Usage is per profile/node/protocol and UTC calendar month. First observations
baseline earlier traffic; month changes clear monthly totals while retaining
counter baselines. Epoch/counter resets are handled explicitly. Failed/stale
reads remain unknown rather than fabricated zero usage. Owner changes never
expose the previous owner's history. Configuration snapshots include collection
policy but exclude usage and sampling state. No browsing history is collected.

`GET /api/v1/me/profiles/{id}/summary` checks ownership.
`GET /api/v1/profiles/{id}/summary` requires `profiles.manage` and supports
administrator reads of another or ownerless profile. Both hide traffic when the
global switch is off. Otherwise traffic is `waiting`, `current` or `unknown`,
with monthly protocol totals and node/protocol breakdowns. The admin profile
shows totals with a separate region-grouped, paginated server view. Protocol
breakdowns use compact bullet-separated rows. This sampled accounting remains
approximate; it is not yet billing or traffic-limit enforcement.


### Installation acceptance fixes (2026-10-04)

Telegram identity resolution now ensures one default VPN profile for each account,
including the initial administrator. The profile uses the Telegram username when
available, or `Admin <id>` / `User <id>` otherwise. Subsequent resolution replaces
only these generated fallback names; custom profile names are preserved. Accounts
and profiles keep independent UUIDs internally so non-Telegram clients remain
possible. The Telegram administration menu exposes Profiles, without a separate
Accounts tab. Adding a Telegram user opens their automatically created profile,
where grants are managed through the existing profile commands.

The PostgreSQL result adapter supports cursor iteration as well as fetch methods.
This fixes access-request settings and traffic reads in member profile summaries.
Optional profile filters explicitly cast null parameters to TEXT for PostgreSQL.
The SSH-key endpoint uses actor permissions and writes a real trailing newline
when recovering a missing public key from an existing private key.

Probe distinguishes agent binary version/commit from protocol runtime version.
The latter identifies deployed runtime assets; an absent runtime directory before
Bootstrap is expected and does not imply an unhealthy agent. Worker completion
followed by another timer-triggered start is also expected: the worker is a
oneshot service scheduled with OnUnitInactiveSec=5s.

PostgreSQL placeholder translation also escapes literal percent signs before
introducing `%s` bind markers. This is necessary for the protocol `LIKE` filter:
psycopg parses percent placeholders even inside SQL string literals. A regression
test uses psycopg's real parser, rather than the SQL fixture substitution.

Opening Requests with no pending items now displays an explicit empty screen;
deciding the last request still returns to the administration menu. Telegram
screen rendering recreates a deleted control message and bounds Rich API calls,
with plain-text fallback when the Rich API is rejected or unavailable. Backend
errors log their class, SQLSTATE, stack locations and request ID without exposing
exception messages, credentials, request bodies or SQL parameters.
