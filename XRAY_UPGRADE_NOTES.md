# Xray-core: 25.12.8 → 26.3.27

Checked on 2026-09-25 against the [official Xray-core releases](https://github.com/XTLS/Xray-core/releases).
Node Plane uses VLESS over REALITY on TCP and XHTTP, with local StatsService
and HandlerService APIs.
The target is `ghcr.io/xtls/xray-core:26.3.27`, the latest release marked
**Latest** by upstream. The newer 26.4–26.9 releases, including 26.9.9, are
marked **Pre-release** and are outside this core upgrade.

| Release | Relevant upstream changes | Node Plane decision |
| --- | --- | --- |
| [26.1.23](https://github.com/XTLS/Xray-core/releases/tag/v26.1.23) | New TUN inbound and Hysteria 2 outbound/transport; clearer REALITY certificate warning; StatsService gained an online-user RPC. | Existing VLESS/REALITY configuration remains; no new transport is enabled. |
| [26.2.6](https://github.com/XTLS/Xray-core/releases/tag/v26.2.6) | New XHTTP obfuscation options; HTTP client headers changed; TLS `allowInsecure` was retired; lower startup memory use. | The server config uses none of the removed TLS options. Keep the existing XHTTP server path. |
| [26.3.27](https://github.com/XTLS/Xray-core/releases/tag/v26.3.27) | XHTTP fixes and lower memory use; REALITY target probing and warnings; API Online Map fix; new Hysteria 2 inbound and Finalmask features. | Pin this stable image. Do not enable unrelated new protocols or parameters during the runtime upgrade. |

The intermediate 26.1/26.2/26.3 patch releases are included in 26.3.27. No
client key, UUID, short ID, SNI, port, or XHTTP path rotation is required by
this image upgrade.

Node Plane now sets the image in the driver-generated node environment and in
all local defaults. On an existing node, use **Apply changes** in node settings:
the driver synchronizes the runtime files and `node.env`, then redeploys Xray.
The deployment pulls and validates the new image before stopping the existing
container and restores that container if startup fails. The existing user
list remains in the mounted `config.json`.

The `x25519` command in this image prints `Password (PublicKey): ...`. Both
initial config generation and later sync now parse that label correctly; the
old parser would have stored `(PublicKey): ...` as part of the public key.
Generation, `xray run -test`, and sync were checked against the actual
`26.3.27` Docker image with a temporary config.
The validation logged an upstream warning for the existing REALITY XHTTP
port `8443` because it is not `443`; the config was accepted.

XHTTP client links now include `mode=auto` and URL-encoded
`extra={"xmux":{"maxConcurrency":"16-32"}}`, matching the Xray VLESS link
format. Reissue an existing user's link to pick up these client settings;
the server does not need reprovisioning. Do not add `allowInsecure` to REALITY
links. If an Android client only connects with that switch enabled, collect
its core version and connection log to diagnose the actual handshake failure.

Profile add/update/remove now persists users in `config.json` and applies the
same change to both live inbounds through HandlerService without a routine
container restart. A one-time redeploy enables HandlerService and replaces
the old file bind mount with a directory mount. The Xray CLI results and live
users are checked before reporting success. A local `26.3.27` container test
verified add, restart recovery from the file, and delete.

Still to verify on a separate node: existing TCP and XHTTP clients passing
traffic after this rollout, migration from an existing file-mounted container,
and rollback.
