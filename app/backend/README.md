# Backend application layer

Status: identity/authorization/opaque credentials and initial HTTP API implemented.
The new Telegram adapter and installer service wiring are not connected.
Current production bot remains unchanged.

Local administration uses the configured PostgreSQL database. Set
NODE_PLANE_SHARED_DIR to the install's shared directory so config loads its .env.
Do not put a DSN/password or bearer token into command-line arguments.

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

The clean target schema adds backend_profiles (UUID, runtime identity, owner,
state/revision), backend_nodes (public node metadata and supported protocols)
and backend_grants. No automatic mirror/import of old profiles/servers is made.
Run init-schema again to create these development tables. Trusted local creation:

```bash
PYTHONPATH=app .venv/bin/python -m backend.admin_cli create-profile --runtime-name alice --display-name Alice --owner-account-id ACCOUNT_UUID
```

GET /api/v1/me/profiles returns only owned profiles with keyset pagination.
GET /api/v1/profiles/{UUID} allows own reads or explicitly authorized admin reads.
GET /api/v1/me/nodes lists only enabled nodes with active owned grants and supported
protocols; frozen/expired profiles do not contribute grants. It returns no SSH
parameters or credentials. Pagination filters ownership before applying limit.
OpenAPI defines safe output models for these routes.

Node registry management is not implemented yet; these
read models are not connected to legacy driver provisioning tables. Local profile
creation does not install runtime or grant VPN access. New state is exercised in
isolated tests; PostgreSQL and real driver integration remain pending.

Before draining a node, bind its future verification target while it is still
active. This immutable binding prevents checking an unrelated empty machine:

```sh
PYTHONPATH=app .venv/bin/python -m backend.admin_cli bind-node-verification-target --node-key NODE --admin-account-id ACCOUNT_UUID --lock-file /opt/node-plane/shared/backend-worker.lock --ssh-target root@node.example
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
containers. The host check is read-only. Bind the actual node host before drain:
the target is administrator-supplied and this experimental backend cannot yet
cross-check it against a durable enrolled host identity. The immutable
retirement record stores the method and target. Custom artifacts, SSH homes
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

POST /api/v1/profiles requires profiles.manage and creates a UUID profile with
an autogenerated immutable runtime name. Input is display_name and optional
owner_account_id. PATCH /profiles/{id} edits display_name, frozen, expires_at;
PATCH /profiles/{id}/grants replaces the whole grant list, requiring grants.manage.
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
Profile deletion is not exposed until remote cleanup has a reliable contract.
Run init-schema again to create the profile command journal and outbox during development.

## Durable operations and runtime intents

Every profile command saves desired state, its replay response, an operation and
per-node/protocol intent snapshots in one transaction. The outbox includes both
current grants and historical targets, so removing a grant cannot lose the task
needed to revoke it. Frozen or already expired profiles produce delete intents;
active grants produce ensure intents. Stable task UUIDs are reserved for driver
idempotency. The executor must process revisions in order or supersede obsolete
intents safely, serialize conflicting work on each node, and reconcile uncertain
outcomes before retrying. Future expiry scheduling is still pending.

GET /api/v1/operations/{id} requires operations.read. The initiating actor can
inspect it; another actor additionally requires profiles.manage. Responses expose
status and task targets/actions, never internal runtime names, intent snapshots
or VPN secrets. HTTP stores durable work only: no background worker is started by the API and
no 202 response claims runtime success. The separate executor records its result. PostgreSQL integration and reconciliation of uncertain outcomes
remain acceptance requirements.

## Explicit driver executor (development)

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
reconciliation of blocked tasks, scheduled expiry enforcement, and config issuance
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
