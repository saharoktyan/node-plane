# Temporary access: node-local foundation

The command journal supports an internal `lease_seconds` option (43200 / 86400 / 259200) on
`ensure` for a new `tmp_<32 lowercase hex digits>` identity. AWG and VLESS commands are
available over the existing authenticated `BackendNodeAction` RPC as
`temporary_ensure` and `temporary_revoke`. Neither user interface nor public
backend issuance is connected yet. Do not treat this foundation as a released
temporary configuration management feature.

The local journal stores a conservative expiry **before** the external ensure.
A successful result records expiry the selected 12 hours, 1 day or 3 days after completion in the journal and
the response's JSON payload. Duplicate requests return that same expiry. An
interrupted ensure leaves durable revocation work, without replaying the ensure.
An existing permanent identity cannot be converted to a temporary identity.
Temporary identities cannot be renewed or converted to permanent peers.

`apply-profile-intent.py expire-leases` processes the persisted deadlines under
the same host lock as profile/settings mutations. It invokes only deletes, marks
failures as pending, and retries those idempotent deletes on the next scan. It
does not need a backend connection. Successful node decommission fences stop
later scans from calling already removed scripts/containers.

The temporary-profile RPC installs persistent `node-plane-lease-expiry.service` and `.timer`
units before journaling/provisioning a peer. The timer is independent of the
agent and controller, runs after boot and schedules the next scan one second
after the previous scan finishes, with one-second systemd accuracy. Scheduler
installation/activation failure prevents provisioning. Uninstallation stops and
removes both units; independent host verification checks they are gone.

Recovery uses the same command and intent through `recover=true`; it never
provisions a missing command or installs scheduling. The receipt includes
`expires_at` and `revocation_mode`, plus `wg_conf` / `vpn_key` for AWG or the
independent `xray_uuid` for VLESS. Revocation requires a journaled temporary
identity, preventing this command path from removing a permanent peer. Older
runtime scripts reject the new action names instead of ignoring the lease.
VLESS uses the existing shared TCP/XHTTP inbounds, REALITY metadata and ports.
Its `revocation_mode` is `new_connections_only`; AWG uses `peer_removed`. These
values describe the protocol operation, not a promise that an offline runtime
has already processed a revoke. Backend/UI status must keep failures visible.

Before exposing issuance:

1. Reconcile deadlines before restarting temporary runtimes. Scans need a working
   local systemd, correct UTC clock and running protocol administration interface.
   A busy shared mutation lock, a stopped Docker daemon or failed deletion delays
   confirmation; the engine retains work and denies retrieval but does not prove
   exact tunnel cutoff during those conditions. Boot-time Docker autostart may
   currently precede the first scan. Do not claim a strict deadline yet.
2. Connect the persisted receipt to backend authorization, retrieval and monitoring.
   Verify actual remote support and enforcement health before enabling issuance.
3. Honor the accepted VLESS limitation: the shared Xray API adapter removes the
   UUID from live authorization and persisted config, but an established ordinary
   connection may continue until disconnect. This also applies to ordinary
   expiring profiles (e.g. 30-day grants) and manual revocation. Before issuance
   and on the config/status card, show the admin: "After expiry or revocation,
   new connections are blocked. Existing connections may continue until they
   disconnect." Do not label credential removal as confirmed session termination.
   Keep shared ports and process. A possible Xray session-closing modification
   is deferred; do not restart shared Xray or create per-user ports/processes.
4. Add backend authorization, idempotent issuance, retrieval/early revocation,
   restore/removal/audit integration and both interfaces. Ordinary Workstation
   users need enrollment separate from privileged SSH administration.
5. Verify with real tunnels, including controller/agent downtime and node reboot.
   For VLESS, test denial of new connections and document continued established
   traffic; for AWG, confirm existing and new traffic stop. Unit tests use mutation
   doubles and do not prove live tunnel behavior.

Source for the Xray limitation:
<https://github.com/XTLS/Xray-core/blob/v26.3.27/proxy/vless/inbound/inbound.go#L232-L236>


## Management interfaces (planned)

Both interfaces open an active temporary-config list with server, protocol /
transport, expiry and actual operation status. Each entry supports viewing /
downloading its config and early revocation. Keep pending/failed revocations
visible; completed entries leave the active list but remain in audit history.

Workstation has a dedicated sidebar tab. Its Create flow selects a server,
protocol when there are multiple protocols, Xray transport when there are
multiple transports, then 12h / 1d / 3d and confirmation. The selected Nodes card
opens this section filtered to that server and skips server selection on Create.
Telegram exposes it only in administrator Settings, with the same conditional
steps and the existing separate-message URI action for iOS. There is no Telegram
server-card entry. VLESS issuance includes the notice above in both interfaces.
