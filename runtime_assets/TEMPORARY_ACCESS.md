# Temporary access

The command journal supports an internal `lease_seconds` option (43200 / 86400 / 259200) on
`ensure` for a new `tmp_<32 lowercase hex digits>` identity. AWG and VLESS commands are
available over the existing authenticated `BackendNodeAction` RPC as
`temporary_ensure`, `temporary_revoke` and read-only `temporary_status`.
The backend registry/API, worker and management interfaces are connected.
Isolated live AWG expiry and Xray authorization/restart checks pass. Two real
Debian 12 QEMU kernel reboots, agent downtime and Docker restart also pass; see
[systemd boot checks](../tests/SYSTEMD_BOOT_CHECK.md). Established Internet VLESS
tunnel acceptance remains.

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
removes the scheduler and protocol supervisor units; independent host verification
checks they are gone.

For each protocol that issues temporary access, a `node-plane-leased-{protocol}.service`
owns container restart instead of Docker's automatic restart policy. It validates
the configured container and bind mount, takes the journal mutation lock, removes
expired or uncertain temporary identities from disk, then starts the stopped
container. A running container is monitored without restarting it. The lock is
released before waiting for container exit. Protocol deployment and rollback also
prune before starting; cleanup disables the supervisors before removing containers.
Permanent peers and other containers keep their existing behavior.

`temporary_status` includes fresh `enforcement_ready` health: the timer and protocol
supervisor must be enabled/active, the expiry service must not be failed, and the
owned protocol container must be running with Docker restart disabled. Backend
activation and downloads require this health, an active lease and the matching
expiry receipt. Older runtimes without this field cannot issue usable credentials.

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

Enforcement boundaries and remaining acceptance:

1. Scans and guarded starts need a working
   local systemd, correct UTC clock and running protocol administration interface.
   A busy shared mutation lock, a stopped Docker daemon or failed deletion delays
   confirmation; the engine retains work and denies retrieval but does not prove
   exact tunnel cutoff during those conditions. Managed container starts prune
   overdue identities before loading configuration. Direct root actions such as
   manually starting a container bypass this supervisor. Do not claim a strict deadline.
2. Backend authorization, retrieval and monitoring use the persisted receipt and
   current remote enforcement health. Failed/unknown outcomes remain visible.
3. Honor the accepted VLESS limitation: the shared Xray API adapter removes the
   UUID from live authorization and persisted config, but an established ordinary
   connection may continue until disconnect. This also applies to ordinary
   expiring profiles (e.g. 30-day grants) and manual revocation. Before issuance
   and on the config/status card, show the admin: "After expiry or revocation,
   new connections are blocked. Existing connections may continue until they
   disconnect." Do not label credential removal as confirmed session termination.
   Keep shared ports and process. A possible Xray session-closing modification
   is deferred; do not restart shared Xray or create per-user ports/processes.
4. Both interfaces use the backend registry described below. Ordinary Workstation
   users need enrollment separate from privileged SSH administration.
5. Verify with real tunnels, including controller/agent downtime and node reboot.
   For VLESS, test denial of new connections and document continued established
   traffic; for AWG, confirm existing and new traffic stop. Disposable Docker tests
   already verify live AWG traffic stops while another peer is retained, and real
   Xray API authorization excludes expired identities after guarded container restart.
   Separate QEMU checks cover real systemd boot and expiry with the agent stopped,
   including startup guards with the expiry timer disabled. They do not cover
   an established REALITY tunnel or every supported host OS.

Source for the Xray limitation:
<https://github.com/XTLS/Xray-core/blob/v26.3.27/proxy/vless/inbound/inbound.go#L232-L236>


## Management interfaces

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

Workstation also offers Copy URI through the native clipboard and Save files.
Saved files live under the workstation state directory in `temporary-configs/<id>`;
on Unix their directory and file permissions are 0700 and 0600. Saving is atomic
and can be repeated for the same configuration. QR is offered only when graphics
support is detected. Windows Terminal must actually answer the Sixel capability
query; WT_SESSION alone never enables it. Unsupported or unresponsive terminals
retain URI and file actions. Real terminal/clipboard and live-node validation
remain separate from automated UI tests.

## Backend registry

Migration revision 3 adds independent temporary configurations and lifecycle
events. It does not alter permanent grants or devices. Only approved
administrators with `settings.manage` can access these endpoints:

- `POST /api/v1/system/temporary-configs`, with `Idempotency-Key` and
  `node_key`, `protocol`, `transport`, `duration_seconds` (43200 / 86400 / 259200).
  Returns a queued operation; remote mutations belong to the worker.
- `GET /api/v1/system/temporary-configs`, with optional `node_key`, `page` and
  `page_size`. Pending and failed work remains visible; completed entries leave
  the list. `GET /{id}` retains their final status.
- `GET /api/v1/system/temporary-configs/{id}/artifact` returns URI/config files,
  exact expiry and `revocation_mode`. It reads the original node receipt and
  checks the live independent identity and current node revision before returning
  anything. Expiry immediately denies retrieval even before worker reconciliation.
- `POST /api/v1/system/temporary-configs/{id}/revoke` cancels an unstarted request
  or queues deletion. It can retry a failed deletion without renewing the lease.

Unknown issuance becomes `blocked` and is reconciled only through read-only
lookup. Failed revocation remains `revoke_blocked`; it is never reported as
confirmed removal. The node timer also settles failed manual deletions and
supersedes only their corresponding interrupted journal entries after successful
removal. Unrelated unfinished operations remain fenced.

Verified node retirement settles temporary access as revoked. Registry-only
retirement records cancellation without claiming host cleanup. Backups exclude
temporary credentials; restoration refuses outstanding temporary access rather
than losing its revocation tracking. Restore clears the terminal registry while
preserving secret-free lifecycle events (administrator account and principal,
node, protocol, action, status and time).
