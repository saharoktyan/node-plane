# Protocol configuration reference

Consolidated: 2026-10-05. Upstream research below was performed on
2026-09-24/25; pins are repository decisions, not claims about today's newest
upstream release. Remaining validation is in
[DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md).

## AmneziaWG

The runtime uses the pinned `amneziavpn/amneziawg-go:3.1.20260828` image with
its digest in `runtime_assets/amnezia-awg/Dockerfile`. Profile generation and
validation live in `runtime_assets/awg_profile.py`. Initialization, regeneration,
container startup, client export and refresh must preserve the same schema.

Retained research sources: [Go release tags](https://github.com/amnezia-vpn/amneziawg-go/tags),
[tools releases](https://github.com/amnezia-vpn/amneziawg-tools/releases),
[pinned upstream Dockerfile](https://github.com/amnezia-vpn/amneziawg-go/blob/v3.1.20260828/Dockerfile),
[parameter/CPS reference](https://docs.amnezia.org/documentation/amnezia-wg/).

The historical migration from 0.2.16 introduced header protection, content
padding and timing ranges, then RandomTrailers/DisableCookies. Later 3.1 fixes
addressed cookie-reply buffers and disabled-cookie behavior under load.
S3/S4, H ranges and I parameters predated that migration. J parameters remain
supported; they were not removed by the 3.1 upgrade.

| Parameter | Configuration rule |
| --- | --- |
| HeaderProtectionKey | Fresh 32-byte key shared by server and clients; S values must be at least 12. |
| S1–S4 | Current generator draws one value in 16–32 and assigns it to all four with RandomTrailers enabled. Equality is deliberate; it does not imply a shared key or identical complete profiles across nodes. |
| H1–H4 | Current profile uses 1, 2, 3, 4 with header protection. |
| RandomTrailers / DisableCookies | `on`/`off` syntax; current starting policy enables trailers and keeps cookies enabled. Disabling cookies changes a DoS defense. |
| I1–I5 | Preserve selected CPS sequences through settings, refresh and every export. Validate tag lengths and actual packet size; do not equate presets with proven DPI invisibility. |
| Padding and timing fields | Validate types/ranges/order; keep conservative defaults unless actual tests justify changes. |
| PersistentKeepalive | Client peer field; ranged values require importer/idle testing rather than unconditional rollout. |

Equal S values with RandomTrailers follow the recommendation preserved from
upstream research. All five I fields, J fields and 3.1 fields must survive
`.conf`, `.vpn`, `vpn://` and embedded config conversion. Startup filtering
must not silently drop fields. Commands such as `wg` execute inside the runtime
container; the host is not required to provide a conflicting implementation.

Issue artifacts against the current applied profile. If a peer was removed by
clean reinstall, re-provision before issuance; do not return a stale cached key.
Preserve peer identity when safely restorable. Changes can invalidate downloaded
configs and require reimport. Keep `.conf` for compatible native clients;
Amnezia's generic name on raw `.conf` import cannot be fixed by a custom comment.
Native exports carry the intended title. Client protocol version and container
image version are different facts.

## Xray

The repository pin is `ghcr.io/xtls/xray-core:26.3.27`. The historical research
compared [26.1.23](https://github.com/XTLS/Xray-core/releases/tag/v26.1.23),
[26.2.6](https://github.com/XTLS/Xray-core/releases/tag/v26.2.6) and
[26.3.27](https://github.com/XTLS/Xray-core/releases/tag/v26.3.27): XHTTP,
HTTP headers, TLS option changes, memory use and API fixes informed the upgrade;
new TUN/Hysteria/Finalmask features were not enabled in Node Plane.

Supported client paths are VLESS/REALITY TCP with Vision flow and XHTTP.
XHTTP links include `mode=auto` and encoded
`extra={"xmux":{"maxConcurrency":"16-32"}}`. REALITY links must contain a
clean base64 public key, without the CLI's `Password (PublicKey):` label.
Do not add TLS `allowInsecure` as a workaround to generated REALITY links.
URI labels identify server, protocol/transport and username; text files are
artifacts, not a promise of automatic import in every client.

`runtime_assets/xray-user-api.py` coordinates persisted `config.json` users
and live HandlerService changes in both inbounds. Ordinary user changes do not
restart the container. A directory mount ensures container restart reads the
updated file. Old containers may need one recreation to enable the API/mount.
Static key/transport settings still require explicit validated application.
File persistence, API results, partial failure and recovery must agree before
success is reported; storing one side alone is insufficient.

StatsService supplies sampled traffic counters. Current runtime accounting is
not a billing/enforcement guarantee. The new admin-only collection policy and
future quotas are separate development tasks.

## Evidence and limits

Recorded live tests include AWG traffic and fresh exports after settings and
reinstall; working XHTTP and TCP on NekoBox; immediate revocation and Xray
user changes without routine restart. iOS/v2rayBox-specific TCP investigation
is deferred by user decision. Comprehensive client/MTU/idle/load and forced
rollback matrices are separate from these successful normal-path tests.
