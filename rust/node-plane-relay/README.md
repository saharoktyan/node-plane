# Node Plane relay

The web cabinet's public entry point. The controller is never exposed: it
dials the relay and holds a gRPC stream open (`proto/relay/v1/link.proto`),
and the relay passes `/api/v1/*` requests over it to the controller API.
The relay has no powers of its own; the API checks every request and, for
requests marked as relayed, refuses adapter credentials, the Telegram
delegation header, integrations and system operations
(`app/backend/relay_policy.py`). Design: `.local/planning/relay-design.md`.

## Run

```sh
cargo test --locked --manifest-path rust/node-plane-relay/Cargo.toml
# public server (behind a TLS reverse proxy that speaks h2c to it)
NODE_PLANE_RELAY_LINK_TOKEN_FILE=token node-plane-relay serve
# on the controller
NODE_PLANE_RELAY_URL=https://cab.example.com NODE_PLANE_RELAY_TOKEN_FILE=token node-plane-relay link
```

| Setting | Purpose |
| --- | --- |
| `NODE_PLANE_RELAY_LISTEN` | `serve` listener; default `127.0.0.1:8443` (HTTP/1.1 and h2c on one port) |
| `NODE_PLANE_RELAY_LINK_TOKEN_FILE` | `serve`: the token a link must present |
| `NODE_PLANE_RELAY_URL` | `link`: the relay; `https://` checks WebPKI certificates |
| `NODE_PLANE_RELAY_TOKEN_FILE` | `link`: the shared token (at least 32 characters) |
| `NODE_PLANE_API_URL` | `link`: the controller API; default `http://127.0.0.1:8080` |

The shared token is temporary: pairing with pinned keys replaces it, along
with built-in TLS and the installer.

## Behaviour

- `serve` answers `/health` (`{"status":"ok","link":"connected"}`) itself and
  `/api/v1/*` over the link; anything else is `404 not_found`.
- Without a link: `503 controller_unavailable`. Limits: request bodies of
  1 MiB (`413 request_too_large`), 64 requests at a time (`503 relay_busy`),
  30 s per request (`504 controller_timeout`). Errors use the contract's
  error body.
- Only allowlisted headers cross in either direction (`src/headers.rs`);
  no `X-Node-Plane-*` header ever does. `link` filters again, reaches only
  `/api/v1/*` and sets `X-Node-Plane-Via: relay` itself. Responses over
  4 MiB become `502 response_too_large`; an unreachable API is
  `502 controller_unavailable`.
- A newer link replaces the older one. `link` reconnects with backoff
  (1 s doubling to 30 s) and keeps the stream alive with HTTP/2 pings.
