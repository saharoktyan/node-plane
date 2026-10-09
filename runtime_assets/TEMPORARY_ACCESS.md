# Temporary access: node-local foundation

The command journal supports an internal `lease_seconds: 86400` option on
`ensure` for a new `tmp_<32 lowercase hex digits>` identity. AWG commands are
available over the existing authenticated `BackendNodeAction` RPC as
`temporary_ensure` and `temporary_revoke`. Neither user interface nor public
backend issuance is connected yet. Do not treat this foundation as a released
24-hour configuration feature.

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

The AWG RPC installs persistent `node-plane-lease-expiry.service` and `.timer`
units before journaling/provisioning a peer. The timer is independent of the
agent and controller, runs after boot and schedules the next scan one second
after the previous scan finishes, with one-second systemd accuracy. Scheduler
installation/activation failure prevents provisioning. Uninstallation stops and
removes both units; independent host verification checks they are gone.

Recovery uses the same command and intent through `recover=true`; it never
provisions a missing command or installs scheduling. The receipt includes
`expires_at`, `wg_conf` and `vpn_key`. Revocation requires a journaled temporary
identity, preventing this command path from removing a permanent peer. Older
runtime scripts reject the new action names instead of ignoring the lease.
VLESS issuance is rejected explicitly, even if its journal engine is exercised
with test doubles.

Before exposing issuance:

1. Reconcile deadlines before restarting temporary runtimes. Scans need a working
   local systemd, correct UTC clock and running protocol administration interface.
   A busy shared mutation lock, a stopped Docker daemon or failed deletion delays
   confirmation; the engine retains work and denies retrieval but does not prove
   exact tunnel cutoff during those conditions. Boot-time Docker autostart may
   currently precede the first scan. Do not claim a strict deadline yet.
2. Connect the persisted receipt to backend authorization, retrieval and monitoring.
   Verify actual remote support and enforcement health before enabling issuance.
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
