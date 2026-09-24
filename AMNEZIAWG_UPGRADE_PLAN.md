# AmneziaWG 2.0 → 3.1: changes and migration plan

Research snapshot: 2026-09-24. This is a plan, not an implementation record.
Node Plane currently builds its AWG container from
`amneziavpn/amneziawg-go:0.2.16`. The newest published upstream image found
at this checkpoint is `3.1.20260828` (`v3.1.20260828` in Git). Pin that
specific release, and resolve its full image digest when implementing; do not
use `latest`. The upstream image bundles `amneziawg-tools` 3.1.20260812.

Sources: [Go tags](https://github.com/amnezia-vpn/amneziawg-go/tags),
[older Go tags](https://github.com/amnezia-vpn/amneziawg-go/tags?after=v0.2.19),
[image tags](https://hub.docker.com/r/amneziavpn/amneziawg-go/tags),
[upstream Dockerfile](https://github.com/amnezia-vpn/amneziawg-go/blob/v3.1.20260828/Dockerfile),
[tools releases](https://github.com/amnezia-vpn/amneziawg-tools/releases),
[protocol reference](https://docs.amnezia.org/documentation/amnezia-wg/),
[client compatibility FAQ](https://docs.amnezia.org/faq/).

Implementation checkpoint (2026-09-24): the repository now pins the 3.1 image,
generates and migrates 3.1 server profiles, preserves `I1`–`I5` and
`Jc/Jmin/Jmax`, issues 3.1 `.conf` and `vpn://`, and refreshes old stored peers
from the live node. Local unit tests and container configuration smoke tests
pass. A real client importing both formats and passing traffic through a
separate node has **not** yet been verified, so production rollout is pending.
`Jc/Jmin/Jmax` were checked against the 3.1 Go README and protocol reference;
they remain supported and are not removed by this upgrade.

## What changed after 0.2.16

| Upstream release | Relevant change | Consequence for Node Plane |
| --- | --- | --- |
| 0.2.17 | Documentation corrected the type of `H1`–`H4`; no runtime feature. | Keep parsing them as non-overlapping `uint32` ranges. |
| 0.2.18 | Fixed missing `S4` padding on keepalive transport packets. | Test idle/keepalive recovery, not only data sent immediately after connection. |
| 0.2.19 | Fixed handling of empty `I1`–`I5`. | Do not depend on this behavior until verified with real clients; current Node Plane generates these values. |
| 3.0.0 | Added ChaCha20 header protection (`HeaderProtectionKey`), random transport content padding (`ContentPaddingAddition`), randomized handshake/keepalive timings, and a range for `PersistentKeepalive`; fixed multiple padding, concurrency, packet accounting and timer bugs. | Extend server and client schemas together. Header protection changes the wire format and requires a matching key on both sides. |
| 3.0.1–3.0.3 | Versioning/build fixes, including pinning the AWG tools version in the Docker build. | Use a matching Go/tools generation; avoid mixing 2.x tools with a 3.x daemon. |
| 3.0.20260805 | Fixed ignored keepalives. | Include idle tunnel tests. |
| 3.1.20260812 | Added `RandomTrailers` and `DisableCookies`. | Add both fields, with an explicit policy and client export. |
| 3.1.20260813 | Updated bundled AWG tools. | Check the actual binaries in the selected image. |
| 3.1.20260814 | Fixed a `RandomTrailers` cookie-reply buffer-size panic. | Do not use the earlier 3.1 image. |
| 3.1.20260828 | Fixed behavior under load when `DisableCookies` is enabled; also includes UDP-window-aware trailer sizing. | Target this release and exercise under-load/large-packet traffic. |

`S3`, `S4`, ranged `H1`–`H4`, and `I1`–`I5` predate our `0.2.16` pin: they
are **not** new 3.1 features. The major migration is the new 3.x parameter
set and synchronized client format.

## 3.x parameter contract

| Field | Where it belongs | Rule for the first Node Plane 3.1 profile |
| --- | --- | --- |
| `HeaderProtectionKey` | Server and each client; same 32-byte key. | Generate once per node using AWG key format, store as a secret, and copy into every client config. With it set, every `S1`–`S4` must be **at least 12**. Prefer `H1=1`, `H2=2`, `H3=3`, `H4=4` as upstream recommends for header protection. |
| `ContentPaddingAddition` | Per endpoint; need not be identical. | Range or fixed value, `uint16`; enable conservatively and validate effective packet size against MTU. |
| `RekeyAfterTime`, `RekeyTimeout`, `RejectAfterTime`, `KeepaliveTimeout`, `MaxHandshakeAttempts` | Per endpoint timers. | Support fixed/ranged values, but start with upstream defaults unless a tested profile demonstrates benefit. Validate order and bounds; do not randomize aggressively by default. |
| `PersistentKeepalive` | Client `[Peer]`; now accepts a range. | Preserve the current fixed `25` initially, then expose ranges only after importer and idle-connection tests. |
| `RandomTrailers` | Server and client behavior. | Enable only with a verified 3.1 client. Use equal `S1`–`S4` as upstream recommends to avoid packet-type ambiguity; check tunnel and outer-path MTU. |
| `DisableCookies` | Per endpoint. | Default `off` initially. Turning it `on` suppresses outgoing cookie replies and reduces a DoS defense; make the tradeoff explicit if later exposed. |
| `I1`–`I5` | Client-side pre-handshake CPS packet sequence; Node Plane currently records it in the server profile and copies it into every client. | **Keep the sequence enabled in the Node Plane 3.1 profile.** Carry all five selected values through generation, settings changes, refresh, `.conf` and `vpn://`; never silently omit them when adding 3.x fields. |

The [official parameter table](https://docs.amnezia.org/documentation/amnezia-wg/#configuration-parameters)
specifies `range<uint16>` for the new padding/timer fields (`a` or `a-b`,
0–65535), `range<uint32>` for `H1`–`H4`, and `on`/`off` for 3.1 switches.
Validate in Node Plane **before** invoking `wg setconf`: malformed or
out-of-range values must fail loudly. Compare the exact accepted syntax with
the pinned `awg` binary because upstream README and docs are not fully
consistent about some range types. Preserve the existing `I1` preset unless
testing justifies changing it; avoid coupling a new camouflage preset to the
version migration. Audit the actual CPS expressions before retaining them:
the [official CPS reference](https://docs.amnezia.org/documentation/amnezia-wg/)
limits each `<r N>`/`<rc N>`/`<rd N>` tag to 1000 bytes/characters. Current
`init-awg.sh` and `regenerate-awg-entropy.sh` generate `<r 1200>` for the
default QUIC preset's `I3`/`I5`, and up to `<r 1400>` in the chaos preset.
Bring those expressions into the supported range while retaining their CPS
sequence and intended traffic shape; validate total UDP packet size against
the route MTU and test the exact output with pinned AWG tools and clients.

### Proposed starting config, based on the user-provided sketch

This is a **client-side illustration**, not a universal upstream preset or a
ready-to-import file. Replace placeholders, derive the address/endpoint/keys
from the node and peer, and include any selected `I1`–`I5` values from the
actual profile. The server must use the same header-protection key and matching
wire-format parameters.

```ini
[Interface]
PrivateKey = <client-private-key>
Address = 10.9.9.2/32
DNS = 1.1.1.1
MTU = 1280
Jc = 4
Jmin = 40
Jmax = 120
S1 = 24
S2 = 24
S3 = 24
S4 = 24
H1 = 1
H2 = 2
H3 = 3
H4 = 4
I1 = <selected-CPS-payload-1>
I2 = <selected-CPS-payload-2>
I3 = <selected-CPS-payload-3>
I4 = <selected-CPS-payload-4>
I5 = <selected-CPS-payload-5>
HeaderProtectionKey = <same-32-byte-key-as-server>
RandomTrailers = on
DisableCookies = off

[Peer]
PublicKey = <server-public-key>
PresharedKey = <peer-preshared-key>
Endpoint = <host>:<port>
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
```

The supplied sketch had `RandomTrailers = 8` and `DisableCookies = true`;
the [official 3.1 reference](https://docs.amnezia.org/documentation/amnezia-wg/#configuration-parameters)
documents `on`/`off` for both. `H1=1` through `H4=4` are **recommended**
when header protection is enabled, not a strict requirement. Its distinct
`S1`–`S4` values satisfy the minimum of 12, but upstream recommends equal
values when `RandomTrailers` is enabled. The supplied key looks like an example;
production must generate a fresh one and must never reuse a key pasted into
documentation or chat. `Jc=4`, `Jmin=40`, `Jmax=120` are candidate values,
not documented canonical defaults; validate them with the pinned daemon and
real traffic before adopting them. `I1`–`I5` are optional at protocol level,
but required by the Node Plane 3.1 profile. The illustrative placeholders
above must be replaced with validated CPS expressions before import.

## Current Node Plane touch points

| Component | Required change |
| --- | --- |
| `runtime_assets/amnezia-awg/Dockerfile`, `deploy-awg.sh`, `node.env.example`; image defaults in Rust driver/agent and `system_reset.py` | Update **all** `0.2.16` pins/defaults and cleanup behavior. Check the new image contains working `amneziawg-go`, `wg`, `wg-quick`, `wg genkey`, `wg setconf`, `wg show`. |
| `runtime_assets/amnezia-awg/start.sh` | Its `strip_conf` whitelist currently discards every new field before `wg setconf`; extend it and verify the live interface reports the expected values. |
| `runtime_assets/init-awg.sh`, `regenerate-awg-entropy.sh`, `show-awg-entropy.sh`, `apply-node-settings.sh` | Introduce a single validated 3.1 profile and a safe migration path. Current S values may be below 12, current H values are custom ranges, and default/chaos CPS tags can exceed the documented `<r>` limit. Retain all five `I` fields, repair invalid tag lengths, and make regeneration an intentional, visible change before reissuing clients. |
| `runtime_assets/awg-add-user.sh` | Read all server-side 3.x fields and write correct client `.conf`; keep peer key/address/PSK handling. Do not add a peer before config generation has been validated, or roll it back on failure. |
| `runtime_assets/conf2vpn.py`, `awg-template.json` | Replace hardcoded `protocol_version: "2"` with version derived from the actual server profile. Export new fields in both the AWG object and `last_config`, and include them in embedded `.conf`. Confirm exact 3.1 JSON key names/representation against a config exported by a current official AmneziaVPN client. |
| `runtime_assets/refresh-awg-config.py`, AWG profile storage and user delivery path | The bot already calls driver refresh before **each** AWG link/file delivery and refuses to issue the old config when refresh fails. Extend refresh to all 3.1 fields and `protocol_version`; tag stored exports with the server config generation/fingerprint and reject a mismatched or incomplete result. Preserve peer identity only if that peer still exists on server; otherwise re-provision the peer before issuing a new config. |
| Driver/agent provisioning and operation status, bot settings UI | Report the deployed **protocol/config version separately from the Docker image version**, show when configs require reimport, and surface partial migration or refresh failures. |

## Migration sequence

1. **Prepare fixtures and compatibility evidence.** Capture an existing 0.2.16
   node config and issued `.conf`/`vpn://` (with dummy secrets), including a
   peer, an idle tunnel, a nonempty `I1`, and current S/H values. Obtain a
   genuine 3.1 export from an official client to lock the JSON format. Test
   whether the new daemon accepts an unchanged AWG2 config; this is useful
   diagnostic information, not a prerequisite for the planned direct migration.
2. **Build and smoke-test the pinned runtime.** Resolve the full image digest,
   build the Node Plane wrapper image, and check `wg setconf`, `wg show`, peer
   addition/removal, container restart, NAT, and interface readiness in an
   isolated test environment. Confirm the userspace Go implementation is used;
   do not silently switch to an older host kernel module.
3. **Implement one complete 3.1 profile.** Add all new fields to the server
   config parser/generator, startup whitelist, client `.conf`, `vpn://`,
   `last_config`, refresh path and validation. Generate S/H/key values that meet
   header-protection constraints. Preserve nonempty `I1`–`I5` through every
   conversion, with CPS lengths validated. Keep the new timing fields
   conservative and `DisableCookies=off` for the first tested profile. Record
   profile/schema version in desired state rather than inferring it solely
   from image tag.
4. **Migrate a test node in place.** Back up server config, node environment,
   peer map, and stored client configs. Precompute and validate the new server
   config and **all** client exports before changing the running container.
   Apply it through the driver/agent, verify the reported live interface, then
   commit the new client exports to storage. A partial failure must leave a
   visible repair state and support restoring the old image/config and old
   exports. Do not claim success when only the container is running.
5. **Reissue existing clients.** User-approved policy: old client configs may
   stop working. Therefore an in-place migration is acceptable; no parallel
   AWG2 listener is required. Mark previously downloaded files/links obsolete.
   On every subsequent bot request, fetch the live server profile, regenerate
   both `.conf` and `vpn://`, validate their 3.1 version and profile generation,
   then save and issue them together. If refresh, import-format generation or
   node access fails, issue **neither** cached format and show a repair path.
   Tell users to reimport. If the server key and peers can be retained, keep
   them; if a peer is gone, create a fresh peer/config instead of reusing its
   old export. A changed obfuscation profile still requires reimport.
6. **Live acceptance on the separate node.** Test current AmneziaVPN
   (official FAQ: 5.0.1.5+), a native AWG 3.1 client, `.conf` import and
   `vpn://` import. Verify not just handshake but DNS, bidirectional traffic,
   idle/keepalive reconnect, large packets/MTU, and an observed pre-handshake
   `I1`–`I5` packet sequence for each selected preset; then second peer add/remove,
   settings apply, clean reinstall, restart and failed-rollout recovery.
   Include local and remote agent transports. Older clients are explicitly
   unsupported for 3.1 configs.
7. **Roll out after evidence.** Upgrade a fresh node first, then existing
   nodes deliberately. Present protocol-version and reimport notice in the bot
   before applying. Keep the previous image/config backup until connectivity
   and issuance pass; document the rollback command/path used by the driver.

Acceptance: a 3.1-capable client can import **both** exports and pass real
traffic; every existing bot profile is either regenerated for the live 3.1
server or visibly marked for re-provisioning; restart/reinstall never silently
returns an obsolete AWG2 config. In a forced-stale-cache test, replacing the
server profile must cause the next bot request to issue a config with the new
profile generation or fail closed, never return the previous `vpn://`/`.conf`.
All five `I` values must survive the same test. The new image tag alone does
not satisfy this.
