# Temporary access implementation

Temporary access uses independent runtime identities and durable leases. Supported durations are 43200, 86400 and 259200 seconds. Backend registry operations require an approved administrator with `settings.manage`.

## Registry and command paths

The backend creates a durable registry entry and dispatches `temporary_ensure`, `temporary_revoke` or read-only `temporary_status` through `BackendNodeAction`. Provisioning uses `tmp_<32 lowercase hex digits>` identities. A permanent identity cannot be converted to a lease.

The node records a conservative expiry before external provisioning and records the selected deadline after successful completion. Duplicate requests return the same deadline. Recovery reads the original command; it does not provision a missing identity or install schedulers.

Unknown issuance remains blocked and is reconciled through observation. Failed revocation remains visible as `revoke_blocked`. Repeated delete attempts must not renew the lease.

## Independent enforcement

`apply-profile-intent.py expire-leases` runs under the node mutation lock. It deletes expired/uncertain temporary identities and retains failed deletion work for retry.

Provisioning installs `node-plane-lease-expiry.service` and `.timer` before issuing credentials. The service runs `monitor-leases`, checking expiry every second in one process and logging only revocations. Failed revocation stops the worker and systemd retries it; the timer also starts the worker after boot or a stop. Updating the scheduler clears the old service's start-limit failure. Runtime synchronization upgrades existing scheduling units without renewing credentials.

`node-plane-leased-{protocol}.service` owns the relevant container restart, pruning expired identities before startup. Docker automatic restart is disabled for supervisor-owned containers.

`temporary_status` reports `enforcement_ready` from scheduler, supervisor and runtime health. Activation/retrieval require a matching live identity, lease receipt and ready enforcement. Decommissioning fences later scans and removes owned scheduling units.

## Artifact and revoke semantics

Artifact retrieval verifies current node state against the original receipt. Expired credentials are denied even before reconciliation. AWG receipts include peer/config artifacts; VLESS uses an independent UUID.

VLESS authorization removal prevents new connections and, on the Node Plane Xray image, closes established ones (`RemoveUser` with `close_connections`). AWG removal deletes the peer (`revocation_mode=peer_removed`).

Each receipt carries `revocation_mode`. For VLESS the agent replaces the runtime script's conservative `new_connections_only` with `connections_closed` when the running Xray implements `CloseUserConnections` on every VLESS inbound (`protocols/xray/revocation.rs`); it probes with an email no profile can have, so nothing closes. An issuance receipt states what a later revocation would do; a fresh revocation receipt states what it did. A recovered revocation keeps the conservative mode, since the image may have changed since. The controller accepts only the modes of the protocol and keeps the latest in `backend_temporary_revocation_modes` (runtime state, not backed up; missing rows read as the conservative mode). Retrieval reports the node's current mode, so a redeployed container changes the card rather than blocking downloads. Agents before this release always report `new_connections_only`. Neither an unavailable runtime nor a failed delete is confirmed tunnel termination. Keep that distinction in backend states and both interfaces.

## Backups and retirement

Configuration backups exclude temporary credentials. Restore checks outstanding access before clearing the registry and preserves secret-free lifecycle events. Verified retirement records revocation; registry-only retirement does not certify host cleanup.

## Tests

Check expiry with agent/controller offline, Docker restart, actual systemd boot, failed delete, duplicate issuance, uncertain ensure and artifact denial after expiry. Use disposable deadlines/identities. Confirm new VLESS connections are denied and established ones close (`test_agent_closes_established_vless_connections_on_real_runtime`); confirm AWG traffic stops while another peer survives.

See [Testing](testing.md) for isolated boot fixtures and [the user guide](../temporary-configs.md) for interface behavior.
