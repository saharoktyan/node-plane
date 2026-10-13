[Русская версия](protocols.ru.md)

# Protocol options

Node Plane supports **VLESS with Xray REALITY** and **AmneziaWG**. Select protocol options in installation defaults for new deployments or in a specific node's settings. Review changes before applying them to a running node.

Changing the shared public address also updates protocol addresses that matched the previous shared address. Distinct VLESS/AWG addresses are preserved as overrides. API settings patches preserve omitted fields; choosing AWG `auto` mode without a port requests a new preset-specific selection. Changes to a running node must be applied before new configurations can be issued; previously downloaded client files must be downloaded and imported again.

If an earlier edit left an old address in the VLESS/AWG address fields, set those fields to the current shared address once and apply the settings. A previously divergent address is indistinguishable from an intentional override.

## Xray / VLESS

| Option | Purpose |
| --- | --- |
| TCP | VLESS/REALITY transport with Vision flow |
| XHTTP | VLESS/REALITY HTTP transport |
| TCP port | TCP listener for the TCP transport; initial default 443 |
| XHTTP port | TCP listener for XHTTP; initial default 8443 |
| SNI | TLS server name, for example `www.cloudflare.com` |
| Fingerprint | Client TLS fingerprint; initial default `chrome` |
| XHTTP path | HTTP path; initial default `/assets` |

Use a valid hostname for SNI, without a scheme, path or port. Client links include the node's address, selected transport and REALITY parameters. Download a fresh configuration after changes to keys, ports or transport settings.

Revoking a VLESS identity blocks new connections. Existing connections may continue until they disconnect.

## AmneziaWG

**Manual profile:** Telegram → node → AWG → Manual parameters, or Workstation → node editor → Manual AWG parameters. Paste a complete `parameter = value` block with Jc/Jmin/Jmax, S1–S4, H1–H4, I1–I5, HeaderProtectionKey, RandomTrailers and DisableCookies. ContentPaddingAddition and the five timing fields are optional; omit them to use engine defaults. Current parameters are available in the editor on reachable nodes. Do not include server or peer identity keys. The backend and agent validate the full combination; changes preserve identities and require fresh client configurations. Type `auto` to return to generation. Entropy regeneration is unavailable while a manual profile is active.

AWG uses a **UDP** port. Auto mode selects a port according to the chosen I-parameter preset and availability; the interface displays the preset's preferred port. Manual mode uses the port you specify. Xray TCP and AWG UDP can use the same numeric port because they use different transports.

I1–I5 describe CPS packet sequences. Choose the QUIC, DNS or chaos preset in the interface. New profiles generate independent packet padding, distinct randomized headers, a header-protection key, content-padding ranges and handshake/rekey timing ranges. Cookie protection stays enabled. These parameters are shared by the server and its clients; the presets do not guarantee connectivity through a particular network.

Updating Node Plane preserves existing AWG 3.1 profiles, including profiles with default timing. New defaults apply to newly initialized interfaces or explicit entropy regeneration. Regeneration preserves server and peer identity keys but changes shared obfuscation parameters: obtain and import fresh client configurations afterwards. Runtime validation rejects excessive padding, nonpositive timing ranges and rekey intervals that overlap the rejection deadline.

The runtime's header-protection key is shared between the server and its clients. Config exports must retain the generated AWG parameters; use a compatible client and a separate device configuration for each client.

Formats include WireGuard-style `.conf` and Amnezia configuration delivery where available. Protocol changes or a clean reinstall can require importing updated files.

## Check a change

1. Verify the public client address and TCP/UDP port reachability.
2. Save and apply the node's settings.
3. Check operation status and runtime health.
4. Obtain a fresh configuration and test from the client network.

The exact container pins are defined by the release's runtime assets. A preset or successful health check does not guarantee connectivity through every network.
