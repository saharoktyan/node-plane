[Русская версия](protocols.ru.md)

# Protocol options

Node Plane supports **VLESS with Xray REALITY** and **AmneziaWG**. Select protocol options in installation defaults for new deployments or in a specific node's settings. Review changes before applying them to a running node.

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

AWG uses a **UDP** port. Auto mode selects a port according to the chosen I-parameter preset and availability; the interface displays the preset's preferred port. Manual mode uses the port you specify. Xray TCP and AWG UDP can use the same numeric port because they use different transports.

I1–I5 describe CPS packet sequences. Choose a supplied preset or validated custom values. Advanced options include junk parameters, packet/header settings, padding and timing. Use the generated defaults unless you have a reason to change them.

The runtime's header-protection key is shared between the server and its clients. Config exports must retain the generated AWG parameters; use a compatible client and a separate device configuration for each client.

Formats include WireGuard-style `.conf` and Amnezia configuration delivery where available. Protocol changes or a clean reinstall can require importing updated files.

## Check a change

1. Verify the public client address and TCP/UDP port reachability.
2. Save and apply the node's settings.
3. Check operation status and runtime health.
4. Obtain a fresh configuration and test from the client network.

The exact container pins are defined by the release's runtime assets. A preset or successful health check does not guarantee connectivity through every network.
