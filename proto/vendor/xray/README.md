# Vendored Xray-core protobuf definitions

Unmodified copies from [XTLS/Xray-core](https://github.com/XTLS/Xray-core),
tag `v26.10.21` (commit `1b1647cd9ddf0abc34b981d4611fc03bcac1ca74`), licensed
under MPL-2.0 (see `LICENSE`). Paths match the upstream repository.

The node agent compiles them into a gRPC client only (`rust/node-agent/build.rs`)
and talks to the pinned runtime image
`ghcr.io/saharoktyan/xray-core:v26.10.31-np.2` from
[the Node Plane fork](https://github.com/saharoktyan/Xray-core/tree/v26.10.31-np.2),
based on upstream `v26.10.31`. The runtime defaults also pin its OCI digest.
These vendored files match upstream `v26.10.21` and `v26.10.31` except for
comments. The fork adds VLESS connection termination to HandlerService; those
proto extensions are not vendored or called by the node agent yet.

Only the import closure of the services the agent uses is vendored:
`HandlerService`, `StatsService` and `RoutingService`. The VLESS account
(`proxy/vless/account.proto`) pulls in the whole transport tree, so the agent
declares the fields it needs (`id`, `flow`, `encryption`) by hand with the same
field numbers.

To update: copy the same files from the new tag, keep this list and the commit
above in sync (also in `licenses/vendored/manifest.json`), regenerate
`THIRD_PARTY_LICENSES` and run the Xray API tests in `tests/test_runtime_docker.py`.
