# Temporary access: node-local foundation

The command journal supports an internal `lease_seconds: 86400` option on
`ensure` for a new `tmp_<32 lowercase hex digits>` identity. It is not yet exposed
by the driver/agent RPC or either user interface. Do not treat this foundation as
a released 24-hour configuration feature.

The local journal stores a conservative expiry **before** the external ensure.
A successful result records expiry 24 hours after completion in the journal and
the response's JSON payload. Duplicate requests return that same expiry. An
interrupted ensure leaves durable revocation work, without replaying the ensure.
An existing permanent identity cannot be converted to a temporary identity.
Temporary identities cannot be renewed or converted to permanent peers.

`apply-profile-intent.py expire-leases` processes the persisted deadlines under
the same host lock as profile/settings mutations. It invokes only deletes, marks
failures as pending, and retries those idempotent deletes on the next scan. It
does not need a backend connection. Successful node decommission fences stop
later scans from calling already removed scripts/containers.

The scan is an engine, **not yet an installed scheduler**. Before exposing issuance:

1. Install an independent node-local scheduler, verify it before enabling a
   temporary peer, and reconcile deadlines before restarting temporary runtimes.
   Define the scheduling tolerance and fail closed when enforcement is unavailable.
2. Carry the lease request and exact persisted receipt through driver/agent RPC.
   Older agents must not silently turn a lease into permanent access.
3. Use a protocol adapter that confirms loss of both new and existing access.
   AWG peer removal updates the running interface and the persisted configuration.
   The shared Xray API adapter removes UUIDs, but is insufficient for an established
   tunnel: VLESS `RemoveUser` in Xray 26.3.27 deletes the validator entry, without
   closing the ordinary connection handled by `Process`. A dedicated temporary
   runtime or another verified per-client termination mechanism is required.
   Do not restart the shared Xray container to expire a temporary credential.
4. Add backend authorization, idempotent issuance, retrieval/early revocation,
   restore/removal/audit integration and both interfaces. Ordinary Workstation
   users need enrollment separate from privileged SSH administration.
5. Verify with real tunnels, including controller/agent downtime and node reboot.
   Unit tests use mutation doubles; they do not prove tunnel termination.

Source for the Xray limitation:
<https://github.com/XTLS/Xray-core/blob/v26.3.27/proxy/vless/inbound/inbound.go#L232-L236>
