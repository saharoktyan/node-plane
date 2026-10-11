# Temporary access implementation

Temporary access uses independent runtime identities and durable leases. Supported durations are 43200, 86400 and 259200 seconds. Backend registry operations require an approved administrator with `settings.manage`.

## Registry and command paths

The backend creates a durable registry entry and dispatches `temporary_ensure`, `temporary_revoke` or read-only `temporary_status` through `BackendNodeAction`. Provisioning uses `tmp_<32 lowercase hex digits>` identities. A permanent identity cannot be converted to a lease.

The node records a conservative expiry before external provisioning and records the selected deadline after successful completion. Duplicate requests return the same deadline. Recovery reads the original command; it does not provision a missing identity or install schedulers.

Unknown issuance remains blocked and is reconciled through observation. Failed revocation remains visible as `revoke_blocked`. Repeated delete attempts must not renew the lease.

## Independent enforcement

`apply-profile-intent.py expire-leases` runs under the node mutation lock. It deletes expired/uncertain temporary identities and retains failed deletion work for retry.

Provisioning installs `node-plane-lease-expiry.service` and `.timer` before issuing credentials. `node-plane-leased-{protocol}.service` owns the relevant container restart, pruning expired identities before startup. Docker automatic restart is disabled for supervisor-owned containers.

`temporary_status` reports `enforcement_ready` from scheduler, supervisor and runtime health. Activation/retrieval require a matching live identity, lease receipt and ready enforcement. Decommissioning fences later scans and removes owned scheduling units.

## Artifact and revoke semantics

Artifact retrieval verifies current node state against the original receipt. Expired credentials are denied even before reconciliation. AWG receipts include peer/config artifacts; VLESS uses an independent UUID and `revocation_mode=new_connections_only`.

VLESS authorization removal prevents new connections; established sessions may survive until disconnect. AWG removal deletes the peer. Neither an unavailable runtime nor a failed delete is confirmed tunnel termination. Keep that distinction in backend states and both interfaces.

## Backups and retirement

Configuration backups exclude temporary credentials. Restore checks outstanding access before clearing the registry and preserves secret-free lifecycle events. Verified retirement records revocation; registry-only retirement does not certify host cleanup.

## Tests

Check expiry with agent/controller offline, Docker restart, actual systemd boot, failed delete, duplicate issuance, uncertain ensure and artifact denial after expiry. Use disposable deadlines/identities. Confirm new VLESS connections are denied and record the established-session limitation; confirm AWG traffic stops while another peer survives.

See [Testing](testing.md) for isolated boot fixtures and [the user guide](../temporary-configs.md) for interface behavior.
