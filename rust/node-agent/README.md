# Node agent

Privileged Rust gRPC executor on each managed node. It reports facts and health, validates ports and runtime settings, manages owned protocol resources and applies explicit profile/node intents.

## Run

```sh
scripts/run_node_agent.sh
cargo test --locked --manifest-path rust/node-agent/Cargo.toml
```

Configuration defaults to `/etc/node-plane/agent.toml`. Environment overrides include `NODE_AGENT_CONFIG_PATH`, `NODE_AGENT_NODE_KEY`, `NODE_AGENT_LISTEN_ADDR` and `NODE_AGENT_HEARTBEAT_SECONDS`.

Mutual TLS is required. `tls_certificate_path`, `tls_key_path` and `tls_client_ca_path` identify the server certificate, private key and client CA under `/etc/node-plane/tls/`. An SSH rollout installs them; a token installation (`scripts/install_agent.sh`) leaves `enrollment.token` there instead, and the agent serves only `NodeEnrollmentService` until the controller proves the token and issues a certificate for the agent's own key (`src/enrollment/`). Missing TLS material without a token prevents startup. See [Node agent on the server](../../docs/agent.md).

## Execution

Profile and node helpers operate under local locks and durable journals. Preserve command identity, intent fingerprints and fences on restart/update. Unknown outcomes require inspection rather than unconditional replay.

Runtime entry points (`init-awg.sh`, `deploy-xray.sh`, `awg-add-user.sh` and the others journals name) are built into the agent (`src/entry.rs`); `node-plane-agent entry NAME [ARG...]` runs one. AWG profiles are generated and checked in `protocols/awg/profile.rs` (`node-plane-agent awg-profile`); the controller checks manual parameters with `app/awg_profile.py`, and both run `tests/fixtures/awg_profile_cases.json`. The delivered runtime bundle holds only data: the version and node.env (an annotated example is `docs/node.env.example`). Container images are pulled by digest and tagged locally (`runtime/images.rs`); the AmneziaWG container runs AmneziaWG's own image with the agent's start script (`protocols/awg/start.sh`) mounted read-only. Check actual runtime facts before reporting installation success. Full decommissioning disables scheduling, removes owned resources, checks the host (`lifecycle/host_check.rs`) and reports it before the agent removes itself.

`UpdateAgent` and `node-plane-agent self-update RELEASE_URL VERSION` install a newer signed release (`src/update/`). The release statement must verify against the key `build.rs` embeds from `release-key.hex` (or `NODE_PLANE_RELEASE_KEY`); without one the agent refuses self-updates. The previous binary stays as `<binary>.previous`, and a transient systemd timer runs it as `update-guard` to restore it unless the new agent confirmed by binding its listener.

`host/hardening.rs` keeps the service's systemd drop-in (`node-plane-agent harden`, also at start); it holds only options the agent's tools tolerate, see [Node agent on the server](../../docs/agent.md#service-hardening).

See [Architecture](../../docs/development/architecture.md), [Temporary access](../../docs/development/temporary-access.md) and [Testing](../../docs/development/testing.md). RPC definitions are under [`proto/`](../../proto/).
